"""ACL snapshot sync: write :AclDocument snapshot nodes, then recompute entity ACLs.

The onyx background worker pushes document-table ACL deltas here. Entity ACL
values are the union over ALL snapshots referenced by the entity's ``biz_id``
(emails / groups union, ``is_public`` OR), applied as an overwrite (SET) so
permission narrowing takes effect. Snapshots are upserted before any
recompute in the same batch, so a first full round never wipes ACLs that
were injected by the ingest path before their snapshot exists.
"""

import datetime
import logging

from neo4j import WRITE_ACCESS

from .config import get_config
from .neo4j import escape_label, get_session

logger = logging.getLogger("magicbox")

# Fixed label outside any workspace label: aggregation state, not KG content.
_SNAPSHOT_LABEL = "AclDocument"

_UPSERT_SNAPSHOT_QUERY = f"""
UNWIND $docs AS d
MERGE (a:{_SNAPSHOT_LABEL} {{doc_id: d.doc_id}})
SET a.external_user_emails = coalesce(d.external_user_emails, []),
    a.external_user_group_ids = coalesce(d.external_user_group_ids, []),
    a.is_public = coalesce(d.is_public, false),
    a.updated_at = $updated_at
RETURN count(a) AS count
"""


def _recompute_query(label: str) -> str:
    """Entity ACL recompute as pure Cypher.

    Affected entities are those whose ``biz_id`` list contains any pushed
    doc_id. Each entity's ACL is then recomputed from ALL snapshots its
    ``biz_id`` references (not just this batch), so the write stays
    self-sufficient for incremental pushes. Documents without a snapshot
    contribute nothing; the overwrite lets narrowing propagate.
    """
    return f"""
UNWIND $doc_ids AS doc_id
MATCH (n:{label})
WHERE doc_id IN coalesce(n.biz_id, [])
WITH DISTINCT n
MATCH (a:{_SNAPSHOT_LABEL})
WHERE a.doc_id IN coalesce(n.biz_id, [])
WITH n,
     reduce(acc = false, a IN collect(a) | acc OR coalesce(a.is_public, false)) AS any_public,
     reduce(acc = [], a IN collect(a) | acc + coalesce(a.external_user_emails, [])) AS all_emails,
     reduce(acc = [], a IN collect(a) | acc + coalesce(a.external_user_group_ids, [])) AS all_group_ids
WITH n, any_public,
     reduce(acc = [], x IN all_emails |
         CASE WHEN x IN acc THEN acc ELSE acc + [x] END) AS merged_emails,
     reduce(acc = [], x IN all_group_ids |
         CASE WHEN x IN acc THEN acc ELSE acc + [x] END) AS merged_groups
SET n.is_public = any_public,
    n.external_user_emails = merged_emails,
    n.external_user_group_ids = merged_groups
RETURN count(n) AS updated
"""


async def sync_acl(docs: list[dict]) -> dict:
    """Idempotently upsert ACL snapshots and recompute affected entity ACLs.

    Returns ``{"docs_updated": n, "entities_updated": m}``.
    """
    if not docs:
        return {"docs_updated": 0, "entities_updated": 0}

    cfg = get_config()
    label = f"`{escape_label(cfg.workspace)}`"
    updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    doc_ids = [doc["doc_id"] for doc in docs]

    async with get_session(default_access_mode=WRITE_ACCESS) as session:
        # Phase 1: upsert every snapshot before any recompute (ordering
        # constraint — the recompute must see the new snapshots).
        result = await session.run(
            _UPSERT_SNAPSHOT_QUERY, docs=docs, updated_at=updated_at
        )
        count = (await result.single())["count"]
        await result.consume()

        # Phase 2: overwrite entity ACLs from the full snapshot set.
        result = await session.run(_recompute_query(label), doc_ids=doc_ids)
        updated = (await result.single())["updated"]
        await result.consume()

    logger.info(
        "acl_sync - docs_updated=%d entities_updated=%d", count, updated
    )
    return {"docs_updated": count, "entities_updated": updated}
