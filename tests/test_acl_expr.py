"""Unit tests for the ACL seat-filter expr builder (task 6.1).

Fail-closed contract: no identity (or both dimensions empty) reduces to
``acl_is_public == true``; null ACL rows evaluate false against every
clause and stay invisible.
"""

import pytest

from lightrag.acl_expr import build_acl_expr, _quote
from lightrag.acl_identity import ACLIdentity

pytestmark = pytest.mark.offline


class TestBuildAclExprWithoutIdentity:
    def test_none_identity_is_public_only(self):
        assert build_acl_expr(None) == "(acl_is_public == true)"

    def test_empty_dimensions_are_public_only(self):
        assert build_acl_expr(ACLIdentity()) == "(acl_is_public == true)"


class TestBuildAclExprWithIdentity:
    def test_emails_only(self):
        expr = build_acl_expr(ACLIdentity(emails=("a@x.com",)))
        assert expr == (
            '(acl_is_public == true) or '
            '(ARRAY_CONTAINS_ANY(acl_external_user_emails, ["a@x.com"]))'
        )

    def test_groups_only(self):
        expr = build_acl_expr(ACLIdentity(group_ids=("grp-1",)))
        assert expr == (
            '(acl_is_public == true) or '
            '(ARRAY_CONTAINS_ANY(acl_external_user_group_ids, ["grp-1"]))'
        )

    def test_both_dimensions(self):
        expr = build_acl_expr(
            ACLIdentity(emails=("a@x.com",), group_ids=("grp-1", "grp-2"))
        )
        assert expr == (
            '(acl_is_public == true) or '
            '(ARRAY_CONTAINS_ANY(acl_external_user_emails, ["a@x.com"])) or '
            '(ARRAY_CONTAINS_ANY('
            'acl_external_user_group_ids, ["grp-1", "grp-2"]))'
        )

    def test_public_clause_always_present(self):
        expr = build_acl_expr(
            ACLIdentity(emails=("a@x.com",), group_ids=("grp-1",))
        )
        assert expr.startswith("(acl_is_public == true)")

    def test_emails_escape_quote_and_backslash(self):
        expr = build_acl_expr(ACLIdentity(emails=('a"b\\c@x.com',)))
        assert 'acl_external_user_emails, ["a\\"b\\\\c@x.com"]' in expr


class TestBuildAclExprIncludePublicFalse:
    def test_public_clause_dropped(self):
        expr = build_acl_expr(
            ACLIdentity(emails=("a@x.com",), include_public=False)
        )
        assert expr == (
            '(ARRAY_CONTAINS_ANY(acl_external_user_emails, ["a@x.com"]))'
        )

    def test_groups_kept_without_public(self):
        expr = build_acl_expr(
            ACLIdentity(group_ids=("grp-1",), include_public=False)
        )
        assert expr == (
            '(ARRAY_CONTAINS_ANY(acl_external_user_group_ids, ["grp-1"]))'
        )

    def test_no_identity_dimensions_and_no_public_is_contradiction(self):
        # No public docs, no explicit shares: nothing is visible. The expr
        # must stay well-formed for Milvus, so it is an always-false
        # conjunction rather than an empty string.
        expr = build_acl_expr(ACLIdentity(include_public=False))
        assert expr == "(acl_is_public == true) and (acl_is_public == false)"


class TestQuote:
    def test_quote_wraps_in_double_quotes(self):
        assert _quote("plain") == '"plain"'

    def test_quote_escapes_backslash_first(self):
        # Backslash must be escaped before the quote so a trailing
        # backslash does not consume the quote escape.
        assert _quote('\\') == '"\\\\"'

    def test_quote_escapes_double_quote(self):
        assert _quote('"') == '"\\""'

    def test_quote_escapes_combined(self):
        assert _quote('a\\"b') == '"a\\\\\\"b"'
