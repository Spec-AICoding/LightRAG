#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-click cleanup of document-related data for the LightRAG deployment.

Cleans document-derived data from all four stores:
  - Redis      : text_chunks / full_docs / entity_chunks / relation_chunks /
                 full_entities / full_relations / llm_response_cache
  - Milvus     : collections named chunks_* / entities_* / relationships_*
  - PostgreSQL : all lightrag_* tables (doc status + KV tables)
  - Neo4j      : nodes/edges of the configured workspace label (default: base)

Configuration-related data is ALWAYS preserved:
  - Neo4j org/permission labels (Person, UserGroup, Department, ResourceGroup,
    Resource, User, Project) are never touched
  - nothing outside the scopes above is deleted

Usage:
  python deployment/script/clean_doc_data.py             # interactive confirm
  python deployment/script/clean_doc_data.py --yes       # skip confirmation
  python deployment/script/clean_doc_data.py --dry-run   # preview only
  python deployment/script/clean_doc_data.py --keep-llm-cache

The script reads connection settings from the repository root .env file
(or from the environment). After a successful cleanup, restart the
lightrag-server so it re-creates the Milvus collections.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Scopes (edit here if the deployment layout changes)
# ---------------------------------------------------------------------------

# Redis key namespaces that hold document-derived data.
REDIS_NAMESPACES = [
    "text_chunks",
    "full_docs",
    "entity_chunks",
    "relation_chunks",
    "full_entities",
    "full_relations",
    "llm_response_cache",
]

# Milvus collection name prefixes that hold document-derived vectors.
MILVUS_COLLECTION_PREFIXES = ("chunks_", "entities_", "relationships_")

# PostgreSQL tables holding document-derived data.
PG_TABLES = [
    "lightrag_doc_status",
    "lightrag_doc_chunks",
    "lightrag_doc_full",
    "lightrag_entity_chunks",
    "lightrag_full_entities",
    "lightrag_full_relations",
    "lightrag_llm_cache",
    "lightrag_relation_chunks",
]

# Neo4j labels that belong to the org/permission configuration graph and
# must NEVER be deleted by this script.
NEO4J_PRESERVE_LABELS = {
    "Person",
    "UserGroup",
    "Department",
    "ResourceGroup",
    "Resource",
    "User",
    "Project",
}


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def load_env_file(path: Path) -> dict:
    """Parse a KEY=VALUE .env file, stripping comments, quotes and spaces."""
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        values[key] = val
    return values


def get_config() -> dict:
    """Resolve connection settings: environment overrides repository .env."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    merged = load_env_file(env_path)
    for key in (
        "REDIS_URI",
        "MILVUS_URI",
        "MILVUS_DB_NAME",
        "MILVUS_USER",
        "MILVUS_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DATABASE",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NEO4J_DATABASE",
        "WORKSPACE",
    ):
        if os.environ.get(key):
            merged[key] = os.environ[key]
    return merged


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

def redis_namespaces(cfg: dict, keep_llm_cache: bool) -> list:
    namespaces = list(REDIS_NAMESPACES)
    if keep_llm_cache:
        namespaces = [n for n in namespaces if n != "llm_response_cache"]
    return namespaces


def scan_redis_keys(cfg: dict, namespaces: list) -> dict:
    """Return {namespace: [keys]} for keys matching each namespace prefix."""
    import redis

    client = redis.Redis.from_url(
        cfg["REDIS_URI"], socket_timeout=30, socket_connect_timeout=10, decode_responses=True
    )
    result = {}
    try:
        for ns in namespaces:
            keys = list(client.scan_iter(match=f"{ns}:*", count=500))
            result[ns] = keys
    finally:
        client.close()
    return result


def delete_redis_keys(cfg: dict, keys_by_ns: dict) -> int:
    """Delete the given keys; returns total number of deleted keys."""
    import redis

    client = redis.Redis.from_url(
        cfg["REDIS_URI"], socket_timeout=30, socket_connect_timeout=10, decode_responses=True
    )
    total = 0
    try:
        for keys in keys_by_ns.values():
            for i in range(0, len(keys), 500):
                pipe = client.pipeline(transaction=False)
                for k in keys[i : i + 500]:
                    pipe.delete(k)
                results = pipe.execute()
                total += sum(1 for r in results if r)
    finally:
        client.close()
    return total


# ---------------------------------------------------------------------------
# Milvus
# ---------------------------------------------------------------------------

def list_milvus_doc_collections(cfg: dict) -> list:
    from pymilvus import MilvusClient

    client = MilvusClient(
        uri=cfg["MILVUS_URI"],
        db_name=cfg.get("MILVUS_DB_NAME", "default"),
        user=cfg.get("MILVUS_USER", ""),
        password=cfg.get("MILVUS_PASSWORD", ""),
        timeout=15,
    )
    try:
        return [
            name
            for name in client.list_collections()
            if name.startswith(MILVUS_COLLECTION_PREFIXES)
        ]
    finally:
        client.close()


def drop_milvus_collections(cfg: dict, names: list) -> None:
    from pymilvus import MilvusClient

    client = MilvusClient(
        uri=cfg["MILVUS_URI"],
        db_name=cfg.get("MILVUS_DB_NAME", "default"),
        user=cfg.get("MILVUS_USER", ""),
        password=cfg.get("MILVUS_PASSWORD", ""),
        timeout=15,
    )
    try:
        for name in names:
            client.drop_collection(name)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

async def _pg_connect(cfg: dict):
    import asyncpg

    return await asyncpg.connect(
        host=cfg.get("POSTGRES_HOST", "localhost"),
        port=int(cfg.get("POSTGRES_PORT", "5432")),
        user=cfg.get("POSTGRES_USER", "postgres"),
        password=cfg.get("POSTGRES_PASSWORD", ""),
        database=cfg.get("POSTGRES_DATABASE", "postgres"),
        timeout=15,
    )


async def pg_table_counts(cfg: dict, tables: list) -> dict:
    conn = await _pg_connect(cfg)
    counts = {}
    try:
        for t in tables:
            rel = await conn.fetchval("SELECT to_regclass($1)", t)
            if rel is None:
                counts[t] = None  # table does not exist
                continue
            counts[t] = await conn.fetchval(f'SELECT count(*) FROM "{t}"')
    finally:
        await conn.close()
    return counts


async def pg_truncate_tables(cfg: dict, tables: list) -> None:
    conn = await _pg_connect(cfg)
    try:
        existing = []
        for t in tables:
            rel = await conn.fetchval("SELECT to_regclass($1)", t)
            if rel is not None:
                existing.append(f'"{t}"')
        if existing:
            await conn.execute(
                "TRUNCATE TABLE " + ", ".join(existing) + " RESTART IDENTITY CASCADE"
            )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Neo4j
# ---------------------------------------------------------------------------

def _neo4j_driver(cfg: dict):
    from neo4j import GraphDatabase

    return GraphDatabase.driver(
        cfg.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(cfg.get("NEO4J_USERNAME", "neo4j"), cfg.get("NEO4J_PASSWORD", "")),
    )


def _neo4j_session(driver, cfg: dict):
    """Open a session on NEO4J_DATABASE when it exists, else the default."""
    db = cfg.get("NEO4J_DATABASE")
    if db:
        try:
            with driver.session(database=db) as s:
                s.run("RETURN 1").consume()
            return driver.session(database=db)
        except Exception:
            pass  # fall back to the server default database
    return driver.session()


def neo4j_count_nodes(driver, cfg: dict, label: str) -> int:
    escaped = label.replace("`", "``")
    with _neo4j_session(driver, cfg) as s:
        row = s.run(f'MATCH (n:`{escaped}`) RETURN count(n) AS c').single()
        return row["c"] if row else 0


def neo4j_delete_label(driver, cfg: dict, label: str) -> int:
    escaped = label.replace("`", "``")
    with _neo4j_session(driver, cfg) as s:
        row = s.run(
            f'MATCH (n:`{escaped}`) DETACH DELETE n RETURN count(n) AS c'
        ).single()
        return row["c"] if row else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_summary(cfg: dict, keep_llm_cache: bool) -> dict:
    """Gather counts for every store (read-only)."""
    summary = {}

    namespaces = redis_namespaces(cfg, keep_llm_cache)
    keys_by_ns = scan_redis_keys(cfg, namespaces)
    summary["redis"] = {ns: len(keys) for ns, keys in keys_by_ns.items() if keys}

    milvus_cols = list_milvus_doc_collections(cfg)
    summary["milvus"] = {name: 0 for name in milvus_cols}

    pg_tables = list(PG_TABLES)
    if keep_llm_cache:
        pg_tables = [t for t in pg_tables if t != "lightrag_llm_cache"]
    summary["pg"] = asyncio.run(pg_table_counts(cfg, pg_tables))

    workspace = cfg.get("WORKSPACE", "") or "base"
    driver = _neo4j_driver(cfg)
    try:
        count = neo4j_count_nodes(driver, cfg, workspace)
        summary["neo4j"] = {"workspace": workspace, "nodes": count}
    finally:
        driver.close()

    return summary


def print_summary(summary: dict) -> None:
    line = "=" * 64
    print(line)
    print("Document-data cleanup summary")
    print(line)

    redis_total = sum(summary["redis"].values())
    print(f"\n[Redis] {redis_total} key(s) to delete")
    for ns, c in sorted(summary["redis"].items()):
        print(f"  {ns:22s} {c}")

    print(f"\n[Milvus] {len(summary['milvus'])} collection(s) to drop")
    for name in sorted(summary["milvus"]):
        print(f"  {name}")

    print(f"\n[PostgreSQL] tables to truncate")
    for t, c in summary["pg"].items():
        if c is None:
            print(f"  {t:28s} (table does not exist, skipped)")
        else:
            print(f"  {t:28s} {c} row(s)")

    n4 = summary["neo4j"]
    print(f"\n[Neo4j] workspace label: {n4['workspace']} -> {n4['nodes']} node(s)")
    print("  Preserved (configuration): " + ", ".join(sorted(NEO4J_PRESERVE_LABELS)))
    print(line)


def execute_cleanup(cfg: dict, summary: dict, keep_llm_cache: bool) -> list:
    """Run the cleanup for all stores; returns list of failure messages."""
    failures = []

    # Redis
    try:
        keys_by_ns = scan_redis_keys(cfg, redis_namespaces(cfg, keep_llm_cache))
        deleted = delete_redis_keys(cfg, keys_by_ns)
        print(f"[Redis] deleted {deleted} key(s)")
    except Exception as e:  # noqa: BLE001
        failures.append(f"Redis cleanup failed: {e}")
        print(f"[Redis] FAILED: {e}")

    # Milvus
    try:
        names = list_milvus_doc_collections(cfg)
        drop_milvus_collections(cfg, names)
        print(f"[Milvus] dropped {len(names)} collection(s): {', '.join(names) or '-'}")
    except Exception as e:  # noqa: BLE001
        failures.append(f"Milvus cleanup failed: {e}")
        print(f"[Milvus] FAILED: {e}")

    # PostgreSQL
    try:
        pg_tables = list(PG_TABLES)
        if keep_llm_cache:
            pg_tables = [t for t in pg_tables if t != "lightrag_llm_cache"]
        asyncio.run(pg_truncate_tables(cfg, pg_tables))
        print(f"[PostgreSQL] truncated {len(pg_tables)} table(s)")
    except Exception as e:  # noqa: BLE001
        failures.append(f"PostgreSQL cleanup failed: {e}")
        print(f"[PostgreSQL] FAILED: {e}")

    # Neo4j
    try:
        workspace = cfg.get("WORKSPACE", "") or "base"
        driver = _neo4j_driver(cfg)
        try:
            deleted = neo4j_delete_label(driver, cfg, workspace)
        finally:
            driver.close()
        print(f"[Neo4j] deleted {deleted} node(s) of label '{workspace}'")
    except Exception as e:  # noqa: BLE001
        failures.append(f"Neo4j cleanup failed: {e}")
        print(f"[Neo4j] FAILED: {e}")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean all document-related data from Redis/Milvus/PostgreSQL/Neo4j."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation prompt",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only preview what would be deleted, do not delete anything",
    )
    parser.add_argument(
        "--keep-llm-cache",
        action="store_true",
        help="keep LLM response caches (Redis llm_response_cache, PG lightrag_llm_cache)",
    )
    args = parser.parse_args()

    cfg = get_config()
    missing = [k for k in ("REDIS_URI",) if not cfg.get(k)]
    if missing:
        print(f"Missing required configuration: {', '.join(missing)} (check .env)")
        return 2

    summary = build_summary(cfg, args.keep_llm_cache)
    print_summary(summary)

    if args.dry_run:
        print("--dry-run: nothing was deleted.")
        return 0

    if not args.yes:
        try:
            answer = input("\nType 'yes' to confirm cleanup: ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            print("Aborted, nothing was deleted.")
            return 1

    print("\nCleaning...")
    failures = execute_cleanup(cfg, summary, args.keep_llm_cache)

    if failures:
        print("\nCompleted with errors:")
        for msg in failures:
            print(f"  - {msg}")
        return 1

    print("\nCleanup completed successfully.")
    print("Remember to restart lightrag-server so it re-creates the Milvus collections.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
