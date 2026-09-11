"""
This module contains S3-compatible object storage routes for the LightRAG API.
"""

import os
import asyncio
import json
import re
import traceback
from pathlib import Path
from typing import Optional, Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict, model_validator

from lightrag import LightRAG
from lightrag.utils import (
    logger,
    generate_track_id,
    validate_workspace,
    compute_mdhash_id,
)
from lightrag.api.utils_api import internal_server_error


class S3ObjectRequest(BaseModel):
    """Request model for a single S3 object."""

    bucket: str = Field(description="S3 bucket name")
    key: str = Field(description="Object key in the bucket")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "bucket": "my-docs",
                "key": "reports/2024/summary.pdf",
            }
        }
    )


class S3IngestRequest(BaseModel):
    """Request model for S3 object ingestion.

    Connection details can be provided in the request body
    or configured via environment variables (S3_ENDPOINT,
    S3_ACCESS_KEY, S3_SECRET_KEY, S3_SECURE).
    """

    endpoint: Optional[str] = Field(
        default=None,
        description="S3 endpoint (e.g., s3.amazonaws.com, oss-cn-hangzhou.aliyuncs.com)",
    )
    access_key: Optional[str] = Field(default=None, description="S3 access key")
    secret_key: Optional[str] = Field(default=None, description="S3 secret key")
    region: Optional[str] = Field(default=None, description="S3 region")
    secure: Optional[bool] = Field(default=None, description="Use HTTPS (default: false)")

    objects: Optional[list[S3ObjectRequest]] = Field(
        default=None, description="Specific objects to download from S3"
    )
    prefix: Optional[str] = Field(
        default=None, description="S3 object key prefix to scan (requires 'bucket')"
    )
    bucket: Optional[str] = Field(
        default=None,
        description="S3 bucket name (required when using 'prefix')",
    )

    process_options: Optional[str] = Field(
        default=None, description="Processing options passthrough (i/t/e/!/F/R/V/P)"
    )
    file_type: Optional[str] = Field(
        default=None,
        description=(
            "Type hint for extensionless files, stored in doc_status metadata. "
            "E.g. 'txt', 'json', 'csv'. When set, the parser routing infers the "
            "parse engine from this value instead of the missing filename suffix."
        ),
    )

    @model_validator(mode="after")
    def validate_object_selection(self):
        has_objects = self.objects is not None and len(self.objects) > 0
        has_prefix = self.prefix is not None

        if not has_objects and not has_prefix:
            raise ValueError("Provide either 'objects' or 'prefix'")
        if has_objects and has_prefix:
            raise ValueError("Provide either 'objects' or 'prefix', not both")
        if has_prefix and not self.bucket:
            raise ValueError("'bucket' is required when using 'prefix'")
        if has_objects:
            for obj in self.objects:
                if not obj.bucket or not obj.key:
                    raise ValueError("Each object requires both 'bucket' and 'key'")
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "prefix": "",
                "bucket": "lightrag-docs",
            }
        }
    )


def _resolve_input_dir(workspace: str = "") -> Path:
    """Resolve the input directory for file downloads.

    Replicates the path logic from ``DocumentManager`` to place
    downloaded files into the correct workspace-scoped directory.
    """
    base = os.environ.get("INPUT_DIR", "./inputs")
    input_dir = Path(base)
    if workspace:
        validate_workspace(workspace)
        input_dir = input_dir / workspace
    input_dir.mkdir(parents=True, exist_ok=True)
    return input_dir


def _resolve_s3_config(request: S3IngestRequest) -> tuple:
    """Resolve S3 connection parameters from request body, falling back to env vars.

    Returns:
        tuple: (endpoint, access_key, secret_key, secure)
    """
    endpoint = request.endpoint or os.environ.get("S3_ENDPOINT_URL")
    access_key = request.access_key or os.environ.get("S3_AWS_ACCESS_KEY_ID")
    secret_key = request.secret_key or os.environ.get("S3_AWS_SECRET_ACCESS_KEY")

    secure = request.secure
    if secure is None:
        secure_str = os.environ.get("S3_SECURE", "false").lower()
        secure = secure_str in ("true", "1", "yes")

    if not endpoint or not access_key or not secret_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "S3 connection details required. Provide endpoint, access_key, and "
                "secret_key in the request body, or set S3_ENDPOINT_URL, S3_AWS_ACCESS_KEY_ID, "
                "S3_AWS_SECRET_ACCESS_KEY in your .env file."
            ),
        )

    return endpoint, access_key, secret_key, secure


def _split_json_objects(file_path: Path) -> tuple[list[str], str]:
    """Split a JSON file into per-object chunk texts.

    A top-level JSON array yields one chunk per element; any other JSON
    document yields a single chunk. Returns ``(chunk_texts, full_text)``.

    Raises:
        ValueError: when the file is not valid UTF-8 JSON.
    """
    full_text = file_path.read_text(encoding="utf-8")
    data = json.loads(full_text)
    if isinstance(data, list):
        chunk_texts = [
            json.dumps(item, ensure_ascii=False) for item in data if item is not None
        ]
    else:
        chunk_texts = [full_text.strip()] if full_text.strip() else []
    return chunk_texts, full_text


async def _ingest_json_as_custom_chunks(
    rag: LightRAG,
    file_path: Path,
    track_id: str,
    file_type: Optional[str] = None,
    max_retries: int = 6,
    retry_delay: float = 5.0,
) -> Optional[str]:
    """Ingest a JSON file as one chunk per JSON object.

    Uses the journaled ``rag.ainsert_custom_chunks`` entry point, so the
    standard doc_status lifecycle, entity/relation extraction and graph
    merge all run unchanged with the real file name passed straight
    through. Afterwards the durable records are patched to carry
    track_id / file_type (and the file name is re-asserted, idempotent).

    Returns:
        The created ``doc_id``, or ``None`` when the file is not
        JSON-ingestible — callers should fall back to the standard pipeline.
    """
    try:
        chunk_texts, full_text = _split_json_objects(file_path)
    except Exception as exc:
        logger.warning(
            "[S3 Ingestion] JSON split failed for %s (%s); falling back to "
            "standard pipeline",
            file_path.name,
            exc,
        )
        return None

    if not chunk_texts:
        logger.warning(
            "[S3 Ingestion] No JSON objects found in %s; falling back to "
            "standard pipeline",
            file_path.name,
        )
        return None

    doc_id = compute_mdhash_id(full_text, prefix="doc-")
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            # Pass the real file name straight through so chunk rows, the
            # extraction snapshot and graph entities carry it from birth
            # (no unknown_source placeholder in Neo4j file_path).
            await rag.ainsert_custom_chunks(
                full_text, chunk_texts, doc_id=doc_id, file_path=file_path.name
            )
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            busy_markers = ("busy", "cannot run concurrently", "scan")
            if not any(marker in str(exc).lower() for marker in busy_markers):
                raise
            if attempt < max_retries:
                logger.info(
                    "[S3 Ingestion] Pipeline busy, retrying custom-chunk "
                    f"ingestion ({attempt}/{max_retries}) for {file_path.name}"
                )
                await asyncio.sleep(retry_delay)
    if last_error is not None:
        raise last_error

    # ainsert_custom_chunks writes a placeholder file_path; patch the
    # durable records so scan matching, document listing and per-doc graph
    # lookups see the real file name.
    try:
        doc = await rag.doc_status.get_by_id(doc_id)
        if doc is not None:
            doc["file_path"] = file_path.name
            doc["track_id"] = track_id
            meta = dict(doc.get("metadata") or {})
            if file_type:
                meta["file_type"] = file_type
            doc["metadata"] = meta
            await rag.doc_status.upsert({doc_id: doc})

            full_doc = await rag.full_docs.get_by_id(doc_id)
            if isinstance(full_doc, dict):
                full_doc["file_path"] = file_path.name
                await rag.full_docs.upsert({doc_id: full_doc})

            chunk_ids = [cid for cid in (doc.get("chunks_list") or []) if cid]
            chunk_rows = await rag.text_chunks.get_by_ids(chunk_ids)
            patch_rows = {}
            for cid, row in zip(chunk_ids, chunk_rows):
                if isinstance(row, dict):
                    patch_rows[cid] = {**row, "file_path": file_path.name}
            if patch_rows:
                await rag.text_chunks.upsert(patch_rows)
    except Exception as patch_exc:
        logger.error(
            f"[S3 Ingestion] Failed to patch document records for "
            f"{file_path.name}: {patch_exc}"
        )

    biz_id_stats = {"pairs": 0, "entities_updated": 0}
    semantic_identifier_stats = {"pairs": 0, "entities_updated": 0}
    try:
        biz_id_stats = await _inject_field_into_entities(
            rag, doc_id, chunk_texts, "id", "biz_id"
        )
    except Exception as biz_exc:
        logger.warning(
            "[S3 Ingestion] biz_id injection failed for %s: %s",
            file_path.name,
            biz_exc,
        )
    try:
        semantic_identifier_stats = await _inject_field_into_entities(
            rag, doc_id, chunk_texts, "semantic_identifier", "semantic_identifier"
        )
    except Exception as sem_exc:
        logger.warning(
            "[S3 Ingestion] semantic_identifier injection failed for %s: %s",
            file_path.name,
            sem_exc,
        )

    connector_stats = {"pairs": 0, "entities_updated": 0}
    connector_source = None
    connector_name = None
    cc_pair_id = _parse_cc_pair_id(file_path.name)
    if cc_pair_id is not None:
        connector_meta = _lookup_connector(cc_pair_id)
        if connector_meta:
            connector_source = connector_meta.get("source")
            connector_name = connector_meta.get("name")
            try:
                connector_stats = await _inject_connector_into_entities(
                    rag,
                    doc_id,
                    chunk_texts,
                    connector_source,
                    connector_name,
                )
            except Exception as conn_exc:
                logger.warning(
                    "[S3 Ingestion] Connector injection failed for %s: %s",
                    file_path.name,
                    conn_exc,
                )
    else:
        logger.info(
            "[S3 Ingestion] Skipping connector injection: %s is not an onyx "
            "batch file (iab_<cc_pair_id>_<attempt>_<batch>.json)",
            file_path.name,
        )

    acl_stats = {"pairs": 0, "entities_updated": 0}
    try:
        acl_stats = await _inject_acl_into_entities(rag, doc_id, chunk_texts)
    except Exception as acl_exc:
        logger.warning(
            "[S3 Ingestion] ACL injection failed for %s: %s",
            file_path.name,
            acl_exc,
        )

    logger.info(
        "[S3 Ingestion] JSON custom chunks: "
        + json.dumps(
            {
                "file": file_path.name,
                "doc_id": doc_id,
                "object_count": len(chunk_texts),
                "biz_id_pairs": biz_id_stats["pairs"],
                "biz_id_entities_updated": biz_id_stats["entities_updated"],
                "semantic_identifier_pairs": semantic_identifier_stats["pairs"],
                "semantic_identifier_entities_updated": semantic_identifier_stats["entities_updated"],
                "connector_source": connector_source,
                "connector_name": connector_name,
                "connector_pairs": connector_stats["pairs"],
                "connector_entities_updated": connector_stats["entities_updated"],
                "acl_pairs": acl_stats["pairs"],
                "acl_entities_updated": acl_stats["entities_updated"],
            },
            ensure_ascii=False,
        )
    )
    return doc_id


def _inject_property_sync(
    uri: str,
    username: str,
    password: str,
    database: Optional[str],
    escaped_label: str,
    property_name: str,
    pairs: list[dict],
) -> int:
    """Blocking Neo4j write: union-merge arrays on matched entities.

    The ``property_name`` is interpolated into backticked identifiers only
    after escaping, so the Cypher stays safe for the internal constants
    used by callers (``biz_id``, ``semantic_identifier``).
    """
    from neo4j import GraphDatabase as Neo4jDriver

    escaped_property = property_name.replace("`", "``")
    query = f"""
    UNWIND $pairs AS p
    MATCH (n:`{escaped_label}`)
    WHERE n.source_id CONTAINS p.chunk_id
    WITH n, collect(DISTINCT p.value) AS new_ids
    WITH n, reduce(acc = coalesce(n.`{escaped_property}`, []), x IN new_ids |
        CASE WHEN x IN acc THEN acc ELSE acc + [x] END) AS merged
    SET n.`{escaped_property}` = merged
    RETURN count(n) AS updated
    """

    driver = Neo4jDriver.driver(uri, auth=(username, password))
    try:
        with driver.session(database=database) as session:
            record = session.run(query, pairs=pairs).single()
            return int(record["updated"]) if record else 0
    finally:
        driver.close()


async def _inject_field_into_entities(
    rag: LightRAG,
    doc_id: str,
    chunk_texts: list[str],
    source_field: str,
    property_name: str,
) -> dict:
    """Inject a JSON field into Neo4j entity properties as a list.

    Entities are matched to chunks through the ``source_id`` property using
    the same document-scoped chunk ids that ``ainsert_custom_chunks`` writes.
    The value is stored as a list property: the existing array and this
    document's values are unioned with deduplication, so values accumulate
    across files — mirroring the ``source_id`` accumulation semantics.

    Args:
        rag: The LightRAG instance.
        doc_id: The document id the chunks belong to.
        chunk_texts: One JSON object string per chunk.
        source_field: JSON field to read (e.g. ``"id"`` for ``biz_id``).
        property_name: Neo4j property to write (e.g. ``"biz_id"``).

    Returns:
        dict: ``{"pairs": n, "entities_updated": m}``.
    """
    from lightrag.utils_pipeline import make_custom_chunk_id

    pairs = []
    for chunk_text in chunk_texts:
        try:
            obj = json.loads(chunk_text)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        value = obj.get(source_field)
        if value is None or value == "":
            continue
        pairs.append(
            {
                "chunk_id": make_custom_chunk_id(doc_id, chunk_text),
                "value": str(value),
            }
        )

    if not pairs:
        return {"pairs": 0, "entities_updated": 0}

    uri, username, password, database, escaped_label = _neo4j_conn_params(rag)

    updated = await asyncio.to_thread(
        _inject_property_sync,
        uri,
        username,
        password,
        database,
        escaped_label,
        property_name,
        pairs,
    )
    return {"pairs": len(pairs), "entities_updated": updated}


def _neo4j_conn_params(rag: LightRAG) -> tuple:
    """Resolve Neo4j connection parameters shared by all injection helpers."""
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    username = os.environ.get("NEO4J_USERNAME", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    database = os.environ.get("NEO4J_DATABASE", "").strip() or None
    label = (os.environ.get("NEO4J_WORKSPACE", "") or rag.workspace or "").strip()
    label = label or "base"
    escaped_label = label.replace("`", "``")
    return uri, username, password, database, escaped_label


# ── Connector / ACL enrichment (magicbox gateway) ─────────────────────────

# Optional trailing dedup suffix: the downloader renames colliding files to
# iab_<cc>_<attempt>_<batch>_<n>.json, which must still resolve the cc_pair_id.
_IAB_FILENAME_RE = re.compile(r"^iab_(\d+)_\d+_\d+(?:_\d+)?\.json$")
CONNECTOR_SOURCE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_CONNECTOR_GATEWAY_URL = os.environ.get(
    "MAGICBOX_CONNECTOR_GATEWAY", "http://127.0.0.1:8090"
).rstrip("/")

_connector_cache: dict[int, Optional[dict]] = {}


def _parse_cc_pair_id(filename: str) -> Optional[int]:
    """Extract the cc_pair_id from onyx batch file names.

    Batch files follow ``iab_{cc_pair_id}_{index_attempt_id}_{batch_num}.json``
    (e.g. ``iab_4_467_1.json`` → 4). Returns ``None`` for any other name.
    """
    match = _IAB_FILENAME_RE.match(filename)
    return int(match.group(1)) if match else None


def _lookup_connector(cc_pair_id: int) -> Optional[dict]:
    """Resolve connector metadata through the magicbox gateway (cached)."""
    if cc_pair_id in _connector_cache:
        return _connector_cache[cc_pair_id]
    result: Optional[dict] = None
    try:
        response = httpx.get(
            f"{_CONNECTOR_GATEWAY_URL}/connectors/{cc_pair_id}", timeout=3.0
        )
        response.raise_for_status()
        result = response.json()
    except Exception as exc:
        logger.warning(
            "[S3 Ingestion] Connector gateway lookup failed for cc_pair_id=%s: %s",
            cc_pair_id,
            exc,
        )
    _connector_cache[cc_pair_id] = result
    return result


def _inject_connector_sync(
    uri: str,
    username: str,
    password: str,
    database: Optional[str],
    escaped_label: str,
    escaped_connector: str,
    source: str,
    connector_name: str,
    chunk_ids: list[str],
) -> int:
    """Blocking Neo4j write: stamp matched entities with connector label + properties."""
    from neo4j import GraphDatabase as Neo4jDriver

    query = f"""
    UNWIND $chunk_ids AS chunk_id
    MATCH (n:`{escaped_label}`)
    WHERE n.source_id CONTAINS chunk_id
    SET n:`{escaped_connector}`,
        n.connector_source = $source,
        n.connector_name = $name
    RETURN count(DISTINCT n) AS updated
    """

    driver = Neo4jDriver.driver(uri, auth=(username, password))
    try:
        with driver.session(database=database) as session:
            record = session.run(
                query,
                chunk_ids=chunk_ids,
                source=source,
                name=connector_name,
            ).single()
            return int(record["updated"]) if record else 0
    finally:
        driver.close()


def _inject_acl_sync(
    uri: str,
    username: str,
    password: str,
    database: Optional[str],
    escaped_label: str,
    pairs: list[dict],
) -> int:
    """Blocking Neo4j write: union-merge ACL fields onto matched entities."""
    from neo4j import GraphDatabase as Neo4jDriver

    query = f"""
    UNWIND $pairs AS p
    MATCH (n:`{escaped_label}`)
    WHERE n.source_id CONTAINS p.chunk_id
    WITH n, collect(p) AS rows
    WITH n,
         reduce(acc = false, r IN rows | acc OR coalesce(r.is_public, false)) AS any_public,
         reduce(acc = [], r IN rows | acc + r.emails) AS all_emails,
         reduce(acc = [], r IN rows | acc + r.group_ids) AS all_group_ids
    WITH n,
         any_public,
         reduce(acc = coalesce(n.external_user_emails, []), x IN all_emails |
             CASE WHEN x IN acc THEN acc ELSE acc + [x] END) AS merged_emails,
         reduce(acc = coalesce(n.external_user_group_ids, []), x IN all_group_ids |
             CASE WHEN x IN acc THEN acc ELSE acc + [x] END) AS merged_groups
    SET n.is_public = coalesce(n.is_public, false) OR any_public,
        n.external_user_emails = merged_emails,
        n.external_user_group_ids = merged_groups
    RETURN count(DISTINCT n) AS updated
    """

    driver = Neo4jDriver.driver(uri, auth=(username, password))
    try:
        with driver.session(database=database) as session:
            record = session.run(query, pairs=pairs).single()
            return int(record["updated"]) if record else 0
    finally:
        driver.close()


async def _inject_connector_into_entities(
    rag: LightRAG,
    doc_id: str,
    chunk_texts: list[str],
    source: str,
    connector_name: str,
) -> dict:
    """Stamp every entity of the document with the connector label and properties.

    The source is applied as a native label only after charset validation
    (labels cannot be parameterized in Cypher). Idempotent: re-running sets
    the same label and properties.
    """
    if not source or not CONNECTOR_SOURCE_RE.match(source):
        logger.warning(
            "[S3 Ingestion] Skipping connector injection: invalid source %r", source
        )
        return {"pairs": 0, "entities_updated": 0}

    from lightrag.utils_pipeline import make_custom_chunk_id

    chunk_ids = [make_custom_chunk_id(doc_id, text) for text in chunk_texts]
    uri, username, password, database, escaped_label = _neo4j_conn_params(rag)
    escaped_connector = source.replace("`", "``")
    updated = await asyncio.to_thread(
        _inject_connector_sync,
        uri,
        username,
        password,
        database,
        escaped_label,
        escaped_connector,
        source,
        connector_name,
        chunk_ids,
    )
    return {"pairs": len(chunk_ids), "entities_updated": updated}


async def _inject_acl_into_entities(
    rag: LightRAG,
    doc_id: str,
    chunk_texts: list[str],
) -> dict:
    """Inject ``external_access`` into entity properties with union semantics.

    ``is_public`` accumulates with OR; email / group lists accumulate as
    deduplicated unions, so entities shared across documents keep every
    grant (see the change design for the shared-entity rationale).
    """
    from lightrag.utils_pipeline import make_custom_chunk_id

    pairs = []
    for chunk_text in chunk_texts:
        try:
            obj = json.loads(chunk_text)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        access = obj.get("external_access")
        if not isinstance(access, dict):
            continue
        pairs.append(
            {
                "chunk_id": make_custom_chunk_id(doc_id, chunk_text),
                "is_public": bool(access.get("is_public")),
                "emails": [
                    str(e) for e in (access.get("external_user_emails") or []) if e
                ],
                "group_ids": [
                    str(g)
                    for g in (access.get("external_user_group_ids") or [])
                    if g
                ],
            }
        )

    if not pairs:
        return {"pairs": 0, "entities_updated": 0}

    uri, username, password, database, escaped_label = _neo4j_conn_params(rag)
    updated = await asyncio.to_thread(
        _inject_acl_sync,
        uri,
        username,
        password,
        database,
        escaped_label,
        pairs,
    )
    return {"pairs": len(pairs), "entities_updated": updated}


def create_s3_routes(
    rag: LightRAG,
    api_key: Optional[str] = None,
) -> APIRouter:
    """Create a router with S3-compatible object storage ingestion endpoints.

    Args:
        rag: The LightRAG instance.
        api_key: Optional API key for authentication.

    Returns:
        APIRouter: Configured router for S3 routes.
    """
    from minio import Minio
    from lightrag.api.routers.document_routes import (
        _reserve_enqueue_slot,
        _release_enqueue_slot,
        pipeline_index_file,
        InsertResponse,
        get_managed_background_tasks,
    )
    from lightrag.kg.shared_storage import start_reserved_background_task

    router = APIRouter(prefix="/documents", tags=["documents"])
    input_dir = _resolve_input_dir(rag.workspace)

    @router.post(
        "/from-s3",
        response_model=InsertResponse,
    )
    async def ingest_from_s3(
        request: S3IngestRequest,
        managed_tasks: set = Depends(get_managed_background_tasks),
    ):
        """
        Ingest documents from any S3-compatible object storage.

        Downloads files from S3 (AWS S3, MinIO, Alibaba OSS, Huawei OBS,
        Tencent COS, etc.) into the configured ``INPUT_DIR``, then triggers
        the standard document processing pipeline.

        **Object Selection:**
        - Use ``objects`` to download specific objects by bucket and key.
        - Use ``prefix`` + ``bucket`` to scan and download all objects
          under a given prefix.

        **S3 Connection:**
        - Provide ``endpoint``, ``access_key``, ``secret_key`` in the
          request body, or configure them via ``S3_ENDPOINT``,
          ``S3_ACCESS_KEY``, ``S3_SECRET_KEY`` in ``.env``.
        - Set ``secure=true`` for HTTPS connections.

        **Returns:**
            InsertResponse with ``track_id`` for monitoring progress via
            ``GET /documents/track_status/{track_id}``.
        """
        enqueue_token = uuid4().hex
        handed_off = False

        try:
            # Resolve S3 config synchronously (fail fast on bad config)
            endpoint, access_key, secret_key, secure = _resolve_s3_config(request)

            # Log request parameters (mask credentials) in JSON format
            masked_secret = (secret_key[:4] + "****") if secret_key else None
            masked_access = (access_key[:4] + "****") if access_key else None
            mode = "objects" if request.objects else "prefix"
            objects_count = len(request.objects) if request.objects else 0
            bucket_display = request.bucket or "-"
            prefix_display = repr(request.prefix) if request.prefix is not None else "-"
            logger.info(
                "[S3 Ingestion] Request received: "
                + json.dumps(
                    {
                        "endpoint": endpoint,
                        "secure": secure,
                        "access_key": masked_access,
                        "secret_key": masked_secret,
                        "mode": mode,
                        "objects_count": objects_count,
                        "bucket": bucket_display,
                        "prefix": prefix_display,
                        "file_type": request.file_type,
                        "process_options": request.process_options,
                    },
                    ensure_ascii=False,
                )
            )
            if request.objects:
                logger.info(
                    "[S3 Ingestion] Request objects: "
                    + json.dumps(
                        [
                            {"bucket": obj.bucket, "key": obj.key}
                            for obj in request.objects
                        ],
                        ensure_ascii=False,
                    )
                )
            elif request.prefix is not None and request.bucket:
                logger.info(
                    "[S3 Ingestion] Request prefix mode: "
                    + json.dumps(
                        {"bucket": request.bucket, "prefix": request.prefix},
                        ensure_ascii=False,
                    )
                )

            # Reserve enqueue slot (same concurrency contract as /upload)
            await _reserve_enqueue_slot(rag, enqueue_token)

            # Build the list of objects to download
            s3_objects: list[tuple[str, str]] = []
            if request.objects:
                s3_objects = [(obj.bucket, obj.key) for obj in request.objects]
            elif request.prefix is not None and request.bucket:
                # List mode — we'll resolve this in the background task
                # so slow list_objects doesn't block the response.
                list_mode = True
                list_bucket = request.bucket
                list_prefix = request.prefix
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Provide either 'objects' or 'prefix' with 'bucket'.",
                )

            track_id = generate_track_id("s3")

            async def _ingest_work(started):
                started.set()
                try:
                    # Create S3 client inside the bg task so the connection
                    # lifecycle is scoped to the download window.
                    # Strip scheme prefix if present — Minio SDK only accepts
                    # host:port, not http://host:port.
                    clean_endpoint = endpoint
                    for prefix in ("http://", "https://"):
                        if clean_endpoint.startswith(prefix):
                            clean_endpoint = clean_endpoint[len(prefix) :]
                            break
                    client: Minio = Minio(
                        endpoint=clean_endpoint,
                        access_key=access_key,
                        secret_key=secret_key,
                        secure=secure,
                    )

                    # Resolve object list
                    resolved_objects: list[tuple[str, str]] = []
                    if s3_objects:
                        resolved_objects = s3_objects
                    else:
                        # list_objects returns an iterator; collect eagerly
                        obj_iter = client.list_objects(
                            list_bucket, prefix=list_prefix, recursive=True
                        )
                        for obj in obj_iter:
                            resolved_objects.append((obj.bucket_name, obj.object_name))

                    if not resolved_objects:
                        logger.info(
                            "[S3 Ingestion] No objects found matching the request"
                        )
                        return

                    # Download each object
                    downloaded_paths: list[Path] = []
                    for bucket, key in resolved_objects:
                        try:
                            filename = Path(key).name or key
                            # Files without an extension use file_type from
                            # the request as their suffix, so the parser
                            # routing picks the correct engine.
                            if not Path(filename).suffix:
                                ext = (request.file_type or "txt").lstrip(".")
                                filename = f"{filename}.{ext}"
                            dest_path = input_dir / filename

                            # Handle same-basename collisions
                            if dest_path.exists():
                                stem = dest_path.stem
                                ext = dest_path.suffix
                                counter = 1
                                while dest_path.exists():
                                    dest_path = input_dir / f"{stem}_{counter}{ext}"
                                    counter += 1

                            await asyncio.to_thread(
                                client.fget_object, bucket, key, str(dest_path)
                            )
                            downloaded_paths.append(dest_path)
                            logger.debug(
                                f"[S3 Ingestion] Downloaded s3://{bucket}/{key} -> {dest_path}"
                            )
                        except Exception as download_error:
                            logger.error(
                                f"[S3 Ingestion] Failed to download s3://{bucket}/{key}: {download_error}"
                            )
                            continue

                    if downloaded_paths:
                        json_ingested = 0
                        for dl_path in downloaded_paths:
                            if dl_path.suffix.lower() == ".json":
                                doc_id = await _ingest_json_as_custom_chunks(
                                    rag,
                                    dl_path,
                                    track_id,
                                    file_type=request.file_type,
                                )
                                if doc_id is not None:
                                    json_ingested += 1
                                    continue
                            await pipeline_index_file(rag, dl_path, track_id)
                        logger.info(
                            f"[S3 Ingestion] Enqueued {len(downloaded_paths)} files "
                            f"from S3 for processing"
                        )
                        if json_ingested:
                            logger.info(
                                f"[S3 Ingestion] {json_ingested} JSON file(s) "
                                f"ingested as per-object custom chunks"
                            )

                        # Store file_type in doc_status metadata if provided
                        if request.file_type:
                            for dl_path in downloaded_paths:
                                found = await rag.doc_status.get_doc_by_file_basename(
                                    dl_path.name
                                )
                                if found is None:
                                    continue
                                if isinstance(found, tuple) and len(found) == 2:
                                    doc_id, doc = found
                                else:
                                    continue
                                existing_meta = dict(doc.get("metadata") or {})
                                existing_meta["file_type"] = request.file_type
                                doc["metadata"] = existing_meta
                                await rag.doc_status.upsert({doc_id: doc})
                except HTTPException:
                    raise
                except Exception as exc:
                    logger.error(f"[S3 Ingestion] S3 connection error: {exc}")
                    logger.error(traceback.format_exc())
                    raise HTTPException(
                        status_code=502,
                        detail=f"S3 connection failed: {exc}",
                    )
                finally:
                    await _release_enqueue_slot(rag, enqueue_token)

            async def _enqueue_backstop():
                await _release_enqueue_slot(rag, enqueue_token)

            await start_reserved_background_task(
                managed_tasks,
                work=_ingest_work,
                backstop_release=_enqueue_backstop,
            )
            handed_off = True

            response = InsertResponse(
                status="success",
                message=(
                    "S3 ingestion started. Files will be downloaded and "
                    "processed in the background."
                ),
                track_id=track_id,
            )
            logger.info(
                "[S3 Ingestion] Response sent: "
                + json.dumps(
                    {
                        "status": response.status,
                        "message": response.message,
                        "track_id": response.track_id,
                    },
                    ensure_ascii=False,
                )
            )
            return response

        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"[S3 Ingestion] Error: {exc}")
            logger.error(traceback.format_exc())
            raise internal_server_error(exc)
        finally:
            if not handed_off:
                await _release_enqueue_slot(rag, enqueue_token)

    return router
