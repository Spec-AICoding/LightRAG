"""Milvus filter-expr builder for the ACL seat filter (fork-custom query-acl).

Builds the recall expr against the three ACL columns shared by the chunks
and entities collections. The relationships collection has no ACL columns,
so callers must never pass this expr to it (see the ``meta_fields`` guard in
``milvus_impl.query``).

Fail-closed semantics: a null ACL row (no ``external_access`` on any
contributing document) evaluates false against every clause here, so it is
invisible under the expr. Without an identity the expr reduces to
``acl_is_public == true`` (public-only). An identity with
``include_public=False`` drops the public clause; with no clause left the
expr is a contradiction (no row matches).
"""

from lightrag.acl_identity import ACLIdentity


def _quote(value: str) -> str:
    """Escape a value into a Milvus string literal (double-quoted)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_acl_expr(identity: ACLIdentity | None) -> str:
    """Build the ACL recall expr for the given requester identity.

    ``None`` (or an identity with empty emails and groups) yields
    ``acl_is_public == true``. Otherwise each non-empty dimension adds an
    ``ARRAY_CONTAINS_ANY`` clause OR-ed together. ``include_public=False``
    drops the public clause; when that leaves no clause (no emails/groups
    either) the expr is an always-false contradiction — the caller sees
    nothing, which is the correct fail-closed result for "no public docs,
    no explicit shares".
    """
    include_public = identity.include_public if identity is not None else True
    clauses = ["acl_is_public == true"] if include_public else []
    if identity is not None:
        if identity.emails:
            quoted = ", ".join(_quote(email) for email in identity.emails)
            clauses.append(
                f"ARRAY_CONTAINS_ANY(acl_external_user_emails, [{quoted}])"
            )
        if identity.group_ids:
            quoted = ", ".join(_quote(gid) for gid in identity.group_ids)
            clauses.append(
                f"ARRAY_CONTAINS_ANY(acl_external_user_group_ids, [{quoted}])"
            )
    if not clauses:
        return "(acl_is_public == true) and (acl_is_public == false)"
    return " or ".join(f"({clause})" for clause in clauses)
