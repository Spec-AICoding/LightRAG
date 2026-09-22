"""Internal ACL-update endpoints (fork-custom query-acl).

Called by the Onyx push path after the Neo4j union update, in order:
chunk rows first (``/acl/chunks``), then the entity recompute chain
(``/acl/entities``) — keeping the authority layer ahead of the seat layer
at every moment. Best-effort by design: a failure here degrades seat
efficiency, never safety (the two-layer table).
"""

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from lightrag import LightRAG
from lightrag.acl_utils import union_acl
from lightrag.api.utils_api import get_combined_auth_dependency
from lightrag.utils import compute_mdhash_id, logger


class ChunkAclUpdateRequest(BaseModel):
    """Update the chunk-row ACL snapshot for one document."""

    biz_doc_id: str = Field(
        min_length=1,
        description="The onyx document id (chunk rows carry it as biz_doc_id).",
    )
    is_public: bool = Field(default=False)
    emails: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)


class EntityAclRecomputeRequest(BaseModel):
    """Recompute the entity union snapshots for one document."""

    biz_doc_id: str = Field(
        min_length=1,
        description="The onyx document id (entity nodes carry it in biz_id).",
    )


def _reverse_lookup_entities_sync(
    uri: str,
    username: str,
    password: str,
    database: Optional[str],
    label: str,
    doc_id: str,
) -> dict[str, list[str]]:
    """Blocking Neo4j reverse lookup: entities whose biz_id contains the doc.

    Mirrors the injection helpers in ``s3_routes``: the label is escaped
    backtick-interpolated; the document id travels as a parameter.
    """
    from neo4j import GraphDatabase as Neo4jDriver

    driver = Neo4jDriver.driver(uri, auth=(username, password))
    try:
        with driver.session(database=database) as session:
            records = session.run(
                f"MATCH (n:`{label}`) WHERE $doc_id IN coalesce(n.biz_id, []) "
                "RETURN n.entity_id AS entity_id, "
                "coalesce(n.biz_id, []) AS biz_ids",
                doc_id=doc_id,
            )
            return {
                record["entity_id"]: list(record["biz_ids"])
                for record in records
                if record["entity_id"]
            }
    finally:
        driver.close()


async def _reverse_lookup_entities(
    rag: LightRAG, doc_id: str
) -> dict[str, list[str]]:
    """Reverse-lookup entities whose ``biz_id`` contains ``doc_id``."""
    from lightrag.api.routers.s3_routes import _neo4j_conn_params

    uri, username, password, database, label = _neo4j_conn_params(rag)
    return await asyncio.to_thread(
        _reverse_lookup_entities_sync,
        uri,
        username,
        password,
        database,
        label,
        doc_id,
    )


def create_acl_routes(
    rag: LightRAG,
    api_key: Optional[str] = None,
) -> APIRouter:
    """Create the fork router with internal ACL-update endpoints.

    Both endpoints are combined_auth protected and fail loud (HTTP 501 /
    500) when the configured VDB backend lacks the fork-custom ACL methods:
    the seat snapshot then stays stale, which costs recall seats only.
    """
    combined_auth = get_combined_auth_dependency(api_key)
    router = APIRouter(prefix="/acl", tags=["acl"])

    @router.post("/chunks", dependencies=[Depends(combined_auth)])
    async def update_chunk_acl(request: ChunkAclUpdateRequest) -> dict[str, Any]:
        """Update the chunk-row ACL snapshot for one document (seat layer).

        Resolves the document's chunk ids via ``biz_doc_id`` and upserts
        the three ACL fields only — content and embeddings are untouched.
        """
        query_ids = getattr(rag.chunks_vdb, "query_ids_by_biz_doc_id", None)
        upsert_acl = getattr(rag.chunks_vdb, "upsert_acl_fields", None)
        if not callable(query_ids) or not callable(upsert_acl):
            raise HTTPException(
                status_code=501,
                detail="chunks VDB backend lacks ACL update support",
            )
        chunk_ids = await query_ids(request.biz_doc_id)
        if chunk_ids:
            acl = {
                "acl_is_public": request.is_public,
                "acl_external_user_emails": request.emails,
                "acl_external_user_group_ids": request.groups,
            }
            await upsert_acl({chunk_id: acl for chunk_id in chunk_ids})
        return {"updated": len(chunk_ids)}

    @router.post("/entities", dependencies=[Depends(combined_auth)])
    async def recompute_entity_acl(
        request: EntityAclRecomputeRequest,
    ) -> dict[str, Any]:
        """Recompute entity union snapshots for one document (seat layer).

        The recompute chain: graph reverse-lookup of entities whose
        ``biz_id`` contains the document, one ACL snapshot per contributing
        document from the chunks collection, OR-merge per entity, then a
        batch upsert of the three ACL fields only. Graph entities are keyed
        by name (``n.entity_id``); the upsert keys are the row primary keys
        ``compute_mdhash_id(name, prefix="ent-")``.
        """
        query_snapshots = getattr(
            rag.chunks_vdb, "query_acl_snapshots_by_biz_doc_ids", None
        )
        upsert_acl = getattr(rag.entities_vdb, "upsert_acl_fields", None)
        if not callable(query_snapshots) or not callable(upsert_acl):
            raise HTTPException(
                status_code=501,
                detail="VDB backend lacks ACL recompute support",
            )

        entity_biz_ids = await _reverse_lookup_entities(rag, request.biz_doc_id)
        if not entity_biz_ids:
            return {"entities": 0}

        all_biz_ids = sorted(
            {
                biz_id
                for biz_ids in entity_biz_ids.values()
                for biz_id in biz_ids
                if biz_id
            }
        )
        snapshots = await query_snapshots(all_biz_ids)

        updates: dict[str, dict[str, Any]] = {}
        for entity_name, biz_ids in entity_biz_ids.items():
            merged: dict[str, Any] | None = None
            for biz_id in biz_ids:
                merged = union_acl(merged, snapshots.get(biz_id))
            if merged is not None:
                # Graph nodes carry the entity NAME (``n.entity_id``); the
                # seat row primary key is ``ent-<md5(name)>``.
                updates[compute_mdhash_id(entity_name, prefix="ent-")] = merged

        if updates:
            await upsert_acl(updates)
        logger.info(
            "ACL entity recompute for %s: %d entities updated",
            request.biz_doc_id,
            len(updates),
        )
        return {"entities": len(updates)}

    return router
