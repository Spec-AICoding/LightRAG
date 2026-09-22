"""Request-scoped ACL identity (fork-custom query-acl).

Holds the querying user's identity for the duration of one query so the
filter layers can read it without threading parameters through the query
pipeline. The REST handlers set it from the request payload and reset it in
a ``finally`` clause; ``milvus_impl.query`` and the filter hook read it.
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class ACLIdentity:
    """The querying user's identity: emails, group ids, and public scope.

    Both collections are deduplicated tuples; an empty tuple means the caller
    carried no value for that dimension. ``include_public=False`` drops the
    ``is_public`` clause from every visibility judgment, so the caller sees
    only rows explicitly shared with them.
    """

    emails: tuple[str, ...] = ()
    group_ids: tuple[str, ...] = ()
    include_public: bool = True


_identity_var: ContextVar[ACLIdentity | None] = ContextVar(
    "query_acl_identity", default=None
)


def _clean(values: list[str] | None) -> tuple[str, ...]:
    """Deduplicate values, dropping None/empty entries, preserving order."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values or []:
        if value and value not in seen:
            seen.add(value)
            cleaned.append(value)
    return tuple(cleaned)


def set_identity(
    emails: list[str] | None = None,
    group_ids: list[str] | None = None,
    include_public: bool = True,
) -> Token:
    """Set the current request's ACL identity; returns a reset token.

    An identity whose both dimensions are empty AND ``include_public`` is
    True is stored as ``None`` (same semantics as "no identity": public-only
    visibility, fail-closed). ``include_public=False`` is preserved even
    without emails/groups — the caller then sees nothing unless explicitly
    shared, because the expr drops the public clause and the empty
    dimensions match no row.
    """
    cleaned_emails = _clean(emails)
    cleaned_group_ids = _clean(group_ids)
    identity = (
        ACLIdentity(
            emails=cleaned_emails,
            group_ids=cleaned_group_ids,
            include_public=include_public,
        )
        if cleaned_emails or cleaned_group_ids or not include_public
        else None
    )
    return _identity_var.set(identity)


def get_identity() -> ACLIdentity | None:
    """Return the current request's ACL identity, or ``None`` when unset."""
    return _identity_var.get()


def serialize_acl_identity(identity: ACLIdentity | None) -> str:
    """Stable string form of an identity, for answer-cache key composition.

    ``None`` serializes to ``"none"`` so anonymous callers keep sharing one
    public-only entry. Anything else pins the entry to the exact (emails,
    group_ids, include_public) triple, so cache entries never cross
    visibility boundaries.
    """
    if identity is None:
        return "none"
    return (
        f"emails={','.join(identity.emails)}"
        f"|groups={','.join(identity.group_ids)}"
        f"|include_public={int(identity.include_public)}"
    )


def reset_identity(token: Token) -> None:
    """Reset the identity ContextVar to the state before ``set_identity``."""
    _identity_var.reset(token)
