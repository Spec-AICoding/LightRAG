"""Unit tests for the request-scoped ACL identity (task 6.6 ContextVar half).

Covers set/get/reset semantics, deduplication, and the "empty identity is
stored as None" fail-closed rule.
"""

import pytest

from lightrag.acl_identity import (
    ACLIdentity,
    get_identity,
    reset_identity,
    serialize_acl_identity,
    set_identity,
)

pytestmark = pytest.mark.offline


class TestSetGetReset:
    def test_set_then_get_returns_identity(self):
        token = set_identity(["a@x.com"], ["grp-1"])
        try:
            identity = get_identity()
            assert identity == ACLIdentity(
                emails=("a@x.com",), group_ids=("grp-1",), include_public=True
            )
        finally:
            reset_identity(token)

    def test_reset_restores_previous_state(self):
        outer = set_identity(["outer@x.com"])
        try:
            inner = set_identity(["inner@x.com"], ["grp"])
            try:
                assert get_identity().emails == ("inner@x.com",)
            finally:
                reset_identity(inner)
            assert get_identity().emails == ("outer@x.com",)
        finally:
            reset_identity(outer)

    def test_default_is_none(self):
        assert get_identity() is None


class TestCleaning:
    def test_deduplicates_and_drops_empty(self):
        token = set_identity(
            ["a@x.com", "a@x.com", "", None, "b@x.com"], ["", "g", "g"]
        )
        try:
            identity = get_identity()
            assert identity.emails == ("a@x.com", "b@x.com")
            assert identity.group_ids == ("g",)
        finally:
            reset_identity(token)

    def test_both_empty_stores_none(self):
        token = set_identity([], [])
        try:
            assert get_identity() is None
        finally:
            reset_identity(token)

    def test_none_arguments_store_none(self):
        token = set_identity(None, None)
        try:
            assert get_identity() is None
        finally:
            reset_identity(token)


class TestIncludePublic:
    def test_default_is_true(self):
        token = set_identity(["a@x.com"])
        try:
            assert get_identity().include_public is True
        finally:
            reset_identity(token)

    def test_false_without_dimensions_is_preserved(self):
        # include_public=False must survive even with no emails/groups,
        # otherwise the "public excluded, nothing shared" case would fall
        # back to public-only visibility.
        token = set_identity([], [], include_public=False)
        try:
            identity = get_identity()
            assert identity is not None
            assert identity.emails == ()
            assert identity.group_ids == ()
            assert identity.include_public is False
        finally:
            reset_identity(token)

    def test_false_with_dimensions(self):
        token = set_identity(["a@x.com"], None, include_public=False)
        try:
            assert get_identity().include_public is False
        finally:
            reset_identity(token)


class TestSerialize:
    def test_none_is_none(self):
        assert serialize_acl_identity(None) == "none"

    def test_identity_includes_all_dimensions(self):
        serialized = serialize_acl_identity(
            ACLIdentity(emails=("a@x.com", "b@x.com"), group_ids=("grp-1",))
        )
        assert serialized == "emails=a@x.com,b@x.com|groups=grp-1|include_public=1"

    def test_include_public_distinguishes_entries(self):
        shared = ACLIdentity(emails=("a@x.com",))
        assert serialize_acl_identity(shared) != serialize_acl_identity(
            ACLIdentity(emails=("a@x.com",), include_public=False)
        )

    def test_distinct_identities_serialize_differently(self):
        assert serialize_acl_identity(ACLIdentity(emails=("a@x.com",))) != (
            serialize_acl_identity(ACLIdentity(emails=("b@x.com",)))
        )
