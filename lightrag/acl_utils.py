"""ACL snapshot helpers (fork-custom query-acl).

Pure functions shared by the ingestion write sites and the ACL recompute
endpoints. Keys follow the Milvus column names (``acl_is_public`` /
``acl_external_user_emails`` / ``acl_external_user_group_ids``); the graph
node property naming (``is_public`` / ``external_user_emails`` /
``external_user_group_ids``) is converted at the edge of this module.
"""

from typing import Any, Mapping

# Canonical shape of an ACL snapshot (Milvus column names).
ACL_FIELDS: tuple[str, ...] = (
    "acl_is_public",
    "acl_external_user_emails",
    "acl_external_user_group_ids",
)


def _dedup_union(*lists: Any) -> list | None:
    """Concatenate lists, dropping duplicates and preserving first-seen order."""
    merged: list = []
    seen: set = set()
    for items in lists:
        for item in items or []:
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged or None


def union_acl(
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Union-merge two ACL snapshots (OR for is_public, set-union for lists).

    ``None`` arguments count as an empty snapshot. A field stays ``None``
    when neither side carries a value (null ACL stays invisible under the
    recall expr — fail-closed). Both mappings use the Milvus column names.
    """
    existing = existing or {}
    incoming = incoming or {}

    existing_public = existing.get("acl_is_public")
    incoming_public = incoming.get("acl_is_public")
    if existing_public is None and incoming_public is None:
        is_public = None
    else:
        is_public = bool(existing_public) or bool(incoming_public)

    return {
        "acl_is_public": is_public,
        "acl_external_user_emails": _dedup_union(
            existing.get("acl_external_user_emails"),
            incoming.get("acl_external_user_emails"),
        ),
        "acl_external_user_group_ids": _dedup_union(
            existing.get("acl_external_user_group_ids"),
            incoming.get("acl_external_user_group_ids"),
        ),
    }


def graph_node_acl_snapshot(node: Mapping[str, Any] | None) -> dict[str, Any]:
    """Convert a graph node's union ACL properties to a Milvus-column snapshot.

    The graph stores the authoritative union under ``is_public`` /
    ``external_user_emails`` / ``external_user_group_ids``; this maps them to
    the ``acl_*`` column names used by the seat layer.
    """
    if not node:
        return dict.fromkeys(ACL_FIELDS)
    return {
        "acl_is_public": node.get("is_public"),
        "acl_external_user_emails": node.get("external_user_emails"),
        "acl_external_user_group_ids": node.get("external_user_group_ids"),
    }
