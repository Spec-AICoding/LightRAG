"""Read-only subgraph retrieval seeded by biz_id."""

import asyncio
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

from ..config import get_config
from ..neo4j import escape_label, get_session, logger

router = APIRouter(tags=["subgraph"])

# Connector sources become native Neo4j labels during ingestion; the same
# charset guard applies here before interpolating a user-provided value.
CONNECTOR_LABEL_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Connection-level failures from the remote Neo4j server (idle TCP resets
# surface as ConnectionResetError/BrokenPipeError; the driver-side wrappers
# are Neo4jError/ServiceUnavailable/SessionExpired). Read-only, so a retry
# is side-effect free.
RETRYABLE_ERRORS = (
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
    ConnectionResetError,
    BrokenPipeError,
)


async def _fetch_with_retry(session, cypher: str, **params):
    """Run a read query, retrying once after a defunct-connection failure.

    The remote Neo4j server resets idle connections; the first query on a
    stale connection fails and the driver reconnects on the next attempt.
    Consumption is included in the protected region because the reset can
    also surface while the result stream is being closed.
    """

    async def fetch():
        result = await session.run(cypher, **params)
        try:
            return [record async for record in result]
        finally:
            await result.consume()

    try:
        return await fetch()
    except RETRYABLE_ERRORS as exc:
        logger.warning(
            "Neo4j query failed (%s), retrying once", type(exc).__name__
        )
        await asyncio.sleep(0.5)
        return await fetch()


@router.get("/subgraph")
async def get_subgraph(
    biz_id: Optional[str] = Query(None, min_length=1, max_length=512),
    # Caps sized to the WebUI graphMaxNodes ceiling (default 1000, up to 2000
    # via the official backend config): the WebUI sends max_nodes=graphMaxNodes
    # and max_relations=2*graphMaxNodes.
    max_nodes: int = Query(50, ge=1, le=2000),
    max_relations: int = Query(100, ge=1, le=4000),
    connector: Optional[str] = Query(None, max_length=64),
    connector_name: Optional[str] = Query(None, max_length=256),
    user_email: Optional[str] = Query(None, max_length=512),
    groups: Optional[str] = Query(None, max_length=1024),
    public_only: bool = Query(False),
    non_public_only: bool = Query(False),
) -> dict:
    """Return the subgraph around entities carrying the given biz_id.

    With ``biz_id`` (document filter) the expansion is scoped to entities
    that ALSO carry the same biz_id — the document's own subgraph. Without
    ``biz_id`` the seeds expand to their full 1-hop neighborhood.

    Optional filters narrow the seeds before expansion:
    - ``biz_id``: exact membership on the biz_id list property; when given,
      also scopes the 1-hop expansion to same-document entities
    - ``connector``: restrict to the connector source label (e.g. ``JIRA``)
    - ``connector_name``: restrict to one connector instance within the
      source (e.g. ``jira-connector2``); parameterized, applied in addition
      to ``connector`` when both are given
    - ``user_email``: keep entities visible to the user email
    - ``groups``: comma-separated external group ids the user belongs to
    - ``public_only``: only entities flagged ``is_public``
    - ``non_public_only``: only entities NOT flagged ``is_public``
      (mutually exclusive with ``public_only``)

    Visibility semantics (union with the public flag, mirroring ingestion):
    - visibility unset: ``is_public OR (email/groups match)`` — public
      entities are always visible; email/groups gate NON-public entities only
    - ``public_only``: ``is_public`` alone (email/groups are irrelevant)
    - ``non_public_only``: ``not is_public``, AND-ed with email/groups when
      given (so ``non_public_only + email`` shows exactly the non-public
      entities visible to that email)

    The ACL condition is applied to BOTH the seeds and their 1-hop
    neighbors — a neighbor without visibility is dropped, so permission
    filtering cannot leak non-public entities through neighbor expansion.

    At least one of ``biz_id`` / ``connector`` / an ACL filter is required.
    ACL-only queries (no biz_id, no connector) seed from ALL workspace
    entities — the WebUI uses this for permission-only filtering.

    Response shape mirrors what the WebUI graph store consumes:
    ``{ nodes: [{id, labels, properties}], edges: [{id, source, target, type, properties}] }``
    """
    cfg = get_config()
    group_list = [g for g in (groups or "").split(",") if g]
    if not biz_id and not connector and not (
        user_email or group_list or public_only or non_public_only
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "At least one filter must be provided: 'biz_id', 'connector', "
                "or an ACL condition ('user_email', 'groups', 'public_only', "
                "'non_public_only')"
            ),
        )
    if public_only and non_public_only:
        raise HTTPException(
            status_code=400,
            detail="'public_only' and 'non_public_only' are mutually exclusive",
        )
    label = f"`{escape_label(cfg.workspace)}`"
    labels = label
    if connector:
        if not CONNECTOR_LABEL_RE.match(connector):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid connector label '{connector}'",
            )
        labels += f":`{escape_label(connector)}`"

    group_list = [g for g in (groups or "").split(",") if g]
    conditions = []
    if biz_id:
        conditions.append("$biz_id IN n.biz_id")
    if connector_name:
        conditions.append("$connector_name = n.connector_name")

    # ACL clause templates parameterized by the node variable (n for seeds,
    # b for neighbors) so the SAME condition gates both.
    acl_templates = []
    if user_email:
        acl_templates.append("$user_email IN coalesce({v}.external_user_emails, [])")
    if group_list:
        acl_templates.append(
            "any(g IN $group_list WHERE g IN coalesce({v}.external_user_group_ids, []))"
        )

    def acl_condition(var: str) -> str:
        """ACL visibility condition for the given node variable ('' = none)."""
        clauses = [t.format(v=var) for t in acl_templates]
        if public_only:
            return f"coalesce({var}.is_public, false)"
        if non_public_only:
            base = f"not coalesce({var}.is_public, false)"
            if clauses:
                return f"({base} AND (" + " OR ".join(clauses) + "))"
            return base
        if clauses:
            return f"(coalesce({var}.is_public, false) OR (" + " OR ".join(clauses) + "))"
        return ""

    seed_acl = acl_condition("n")
    if seed_acl:
        conditions.append(seed_acl)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    neighbor_acl = acl_condition("b")
    # With biz_id (document filter) the 1-hop expansion is scoped to entities
    # carrying the SAME biz_id, so the result is the document's own subgraph —
    # hub entities can no longer drag in neighbors from unrelated documents.
    # Without biz_id (connector/ACL-only filters) the full 1-hop expansion
    # applies, preserving the previous behavior.
    neighbor_scope = " AND $biz_id IN b.biz_id" if biz_id else ""
    neighbor_where = (
        f"{neighbor_scope}{(' AND ' + neighbor_acl) if neighbor_acl else ''}"
    )

    async with get_session() as session:
        # 1. Seeds: entities carrying the biz_id (and optional filters)
        seeds = await _fetch_with_retry(
            session,
            f"""
            MATCH (n:{labels})
            {where}
            RETURN properties(n) AS node, labels(n) AS node_labels
            ORDER BY n.entity_id
            LIMIT $max_nodes
            """,
            biz_id=biz_id,
            max_nodes=max_nodes,
            connector_name=connector_name,
            user_email=user_email,
            group_list=group_list,
        )
        # REAL Neo4j labels (workspace + connector source) per seed entity,
        # surfaced in the response so the UI shows what storage actually holds.
        # Built BEFORE `seeds` is reassigned to the bare property dicts below.
        seed_labels = {
            record["node"]["entity_id"]: record["node_labels"]
            for record in seeds
        } if seeds else {}
        seeds = [record["node"] for record in seeds]

        if not seeds:
            return {"nodes": [], "edges": []}

        seed_ids = [node["entity_id"] for node in seeds]

        # 2. One-hop neighbors across DIRECTED relations. The neighbor side
        # carries the SAME ACL condition as the seeds, so non-visible
        # entities can never leak through neighbor expansion.
        rows = await _fetch_with_retry(
            session,
            f"""
            MATCH (a:{label})-[r:DIRECTED]-(b:{label})
            WHERE a.entity_id IN $seed_ids{neighbor_where}
            RETURN a.entity_id AS source, b.entity_id AS target,
                   elementId(r) AS rel_id, properties(r) AS props,
                   properties(b) AS neighbor, labels(b) AS neighbor_labels
            """,
            seed_ids=seed_ids,
            biz_id=biz_id,
            user_email=user_email,
            group_list=group_list,
        )

    # 3. Assemble nodes (seeds first, then neighbors) and edges, capped
    nodes: dict[str, dict] = {}
    for node in seeds:
        nodes[node["entity_id"]] = node

    edges: list[dict] = []
    for row in rows:
        neighbor_id = row["neighbor"].get("entity_id")
        if neighbor_id is not None and len(nodes) < max_nodes:
            nodes.setdefault(neighbor_id, row["neighbor"])
            seed_labels.setdefault(neighbor_id, row["neighbor_labels"])
        if len(edges) >= max_relations:
            continue
        edges.append(
            {
                "id": row["rel_id"],
                "source": row["source"],
                "target": row["target"],
                "type": "DIRECTED",
                "properties": row["props"],
            }
        )

    return {
        "nodes": [
            {
                "id": nid,
                # The REAL Neo4j labels (workspace + connector source), or the
                # workspace alone as a defensive fallback.
                "labels": seed_labels.get(nid) or [cfg.workspace],
                "properties": props,
            }
            for nid, props in nodes.items()
        ],
        "edges": edges,
    }
