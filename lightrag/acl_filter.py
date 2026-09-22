"""ACL filter hook (fork-custom query-acl).

Implements the post-recall visibility judgment: the ② chunk lineage
collapse query and the ③ entity/relationship authority judgment. The hook
is invoked from ``_build_query_context`` via a ContextVar-registered
callback; upstream runs with the callback unset and therefore unchanged.

The visibility judgment lives in exactly one place — the Milvus expr —
and this module only computes set differences and applies them.
"""

from typing import Any, Callable, Awaitable

from contextvars import ContextVar

from lightrag.acl_identity import ACLIdentity, get_identity
from lightrag.constants import GRAPH_FIELD_SEP
from lightrag.utils import logger

FilterCallback = Callable[
    [dict[str, Any], list[dict], Any], Awaitable[tuple[dict[str, Any], list[dict]]]
]

_callback_var: ContextVar[FilterCallback | None] = ContextVar(
    "acl_filter_callback", default=None
)


def register_filter_callback(callback: FilterCallback) -> None:
    """Register the filter callback for the current context.

    Set once at server startup (fork deployment); the request tasks inherit
    it from the startup context. ``None`` would re-disable filtering.
    """
    _callback_var.set(callback)


def get_filter_callback() -> FilterCallback | None:
    """Return the registered callback, or ``None`` when unset."""
    return _callback_var.get()


def _entity_visible(entity: dict[str, Any], identity: ACLIdentity | None) -> bool:
    """Authority judgment for one entity (graph union ACL properties).

    The node data already carries the union properties (``is_public`` /
    ``external_user_emails`` / ``external_user_group_ids``); an entity is
    visible when public (unless the requester opted out via
    ``include_public=False``), or when the requester shares an email or
    group. Missing properties mean invisible (fail-closed).
    """
    include_public = identity.include_public if identity is not None else True
    if include_public and entity.get("is_public"):
        return True
    if identity is None:
        return False
    if set(identity.emails) & set(entity.get("external_user_emails") or []):
        return True
    if set(identity.group_ids) & set(entity.get("external_user_group_ids") or []):
        return True
    return False


def _relation_lineage_ids(relation: dict[str, Any]) -> list[str]:
    """Normalize a relation's ``source_id`` lineage to a list of chunk ids.

    The graph stores ``source_id`` as a <SEP>-joined string (or a bare
    single chunk id); a list form is tolerated for direct callers. Without
    this normalization the string would be iterated character by character
    and every lineage match would fail, hiding all relations.
    """
    raw = relation.get("source_id")
    if not raw:
        return []
    if isinstance(raw, str):
        return [part for part in raw.split(GRAPH_FIELD_SEP) if part]
    return [str(part) for part in raw if part]


def _relation_visible(
    relation: dict[str, Any], visible_ids: set[str]
) -> bool:
    """Authority judgment for one relation: at least one lineage chunk visible.

    A relation without ``source_id`` lineage has no evidence of visibility
    and is hidden (fail-closed).
    """
    return any(
        chunk_id in visible_ids for chunk_id in _relation_lineage_ids(relation)
    )


async def apply_acl_filter(
    truncation_result: dict[str, Any],
    merged_chunks: list[dict],
    chunks_vdb: Any,
) -> tuple[dict[str, Any], list[dict]]:
    """Run the ② collapse query and ③ authority judgment, then apply both.

    Returns the filtered ``(truncation_result, merged_chunks)``. The
    truncation result keeps its shape; every entity/relation/mapping entry
    that fails the judgment is removed, and ``merged_chunks`` keeps only
    chunks whose id is in the visible set.
    """
    identity = get_identity()

    # ② Chunk lineage collapse: candidate ids are the merged chunk ids plus
    # every recalled relation's source_id chunks; one Milvus query decides
    # the visible subset (fail-closed: absent rows are invisible).
    candidate_ids: set[str] = {
        chunk["chunk_id"] for chunk in merged_chunks if chunk.get("chunk_id")
    }
    for relation in truncation_result.get("filtered_relations", []):
        candidate_ids.update(_relation_lineage_ids(relation))

    visible_ids: set[str] = set()
    if candidate_ids:
        query_visible = getattr(chunks_vdb, "query_acl_visible_ids", None)
        if callable(query_visible):
            visible_ids = set(await query_visible(sorted(candidate_ids)))
        else:
            # Non-Milvus backend has no ACL columns; keep nothing (fail-closed).
            logger.warning(
                "ACL filter active but chunks VDB lacks query_acl_visible_ids; "
                "all chunks and lineage relations are hidden (fail-closed)"
            )

    # ③ Authority judgment on entities and relations.
    visible_entity_names = {
        entity.get("entity_name")
        for entity in truncation_result.get("filtered_entities", [])
        if entity.get("entity_name") and _entity_visible(entity, identity)
    }
    visible_relation_pairs: set[tuple[str, str]] = set()
    for relation in truncation_result.get("filtered_relations", []):
        src, tgt = relation.get("src_id"), relation.get("tgt_id")
        if src is None or tgt is None:
            src, tgt = relation.get("src_tgt", (None, None))
        if src is not None and tgt is not None and _relation_visible(
            relation, visible_ids
        ):
            visible_relation_pairs.add((src, tgt))

    def _filter_entities() -> None:
        entities_context = [
            entry
            for entry in truncation_result.get("entities_context", [])
            if entry.get("entity") in visible_entity_names
        ]
        filtered_entities = [
            entity
            for entity in truncation_result.get("filtered_entities", [])
            if entity.get("entity_name") in visible_entity_names
        ]
        entity_id_to_original = {
            name: entity
            for name, entity in truncation_result.get(
                "entity_id_to_original", {}
            ).items()
            if name in visible_entity_names
        }
        truncation_result["entities_context"] = entities_context
        truncation_result["filtered_entities"] = filtered_entities
        truncation_result["entity_id_to_original"] = entity_id_to_original

    def _filter_relations() -> None:
        relations_context = [
            entry
            for entry in truncation_result.get("relations_context", [])
            if (entry.get("entity1"), entry.get("entity2"))
            in visible_relation_pairs
        ]
        filtered_relations = []
        for relation in truncation_result.get("filtered_relations", []):
            src, tgt = relation.get("src_id"), relation.get("tgt_id")
            if src is None or tgt is None:
                src, tgt = relation.get("src_tgt", (None, None))
            if (src, tgt) in visible_relation_pairs:
                filtered_relations.append(relation)
        relation_id_to_original = {
            pair: relation
            for pair, relation in truncation_result.get(
                "relation_id_to_original", {}
            ).items()
            if pair in visible_relation_pairs
        }
        truncation_result["relations_context"] = relations_context
        truncation_result["filtered_relations"] = filtered_relations
        truncation_result["relation_id_to_original"] = relation_id_to_original

    _filter_entities()
    _filter_relations()
    merged_chunks = [
        chunk for chunk in merged_chunks if chunk.get("chunk_id") in visible_ids
    ]

    logger.info(
        f"ACL filter: {len(visible_ids)} visible chunks, "
        f"{len(visible_entity_names)} visible entities, "
        f"{len(visible_relation_pairs)} visible relations"
    )
    return truncation_result, merged_chunks
