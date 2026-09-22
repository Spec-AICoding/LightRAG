"""Unit tests for the ACL snapshot helpers (tasks 6.1/6.7 pure-function half).

Covers ``union_acl`` OR-merge semantics, ``graph_node_acl_snapshot``
naming conversion, and the ``_extract_acl`` chunk-JSON flattening helper
(with / without / malformed ``external_access``).
"""

import pytest

from lightrag.acl_utils import (
    ACL_FIELDS,
    graph_node_acl_snapshot,
    union_acl,
)
from lightrag.lightrag import _extract_acl

pytestmark = pytest.mark.offline


class TestUnionAcl:
    def test_both_none_yields_all_none(self):
        merged = union_acl(None, None)
        assert merged == {
            "acl_is_public": None,
            "acl_external_user_emails": None,
            "acl_external_user_group_ids": None,
        }

    def test_none_is_empty_snapshot(self):
        incoming = {
            "acl_is_public": True,
            "acl_external_user_emails": ["a@x.com"],
            "acl_external_user_group_ids": None,
        }
        assert union_acl(None, incoming) == incoming

    def test_is_public_is_or(self):
        assert (
            union_acl({"acl_is_public": True}, {"acl_is_public": False})[
                "acl_is_public"
            ]
            is True
        )
        assert (
            union_acl({"acl_is_public": False}, {"acl_is_public": None})[
                "acl_is_public"
            ]
            is False
        )

    def test_lists_union_and_dedup_preserving_order(self):
        merged = union_acl(
            {"acl_external_user_emails": ["a@x.com", "b@x.com"]},
            {"acl_external_user_emails": ["b@x.com", "c@x.com"]},
        )
        assert merged["acl_external_user_emails"] == [
            "a@x.com",
            "b@x.com",
            "c@x.com",
        ]

    def test_group_ids_union(self):
        merged = union_acl(
            {"acl_external_user_group_ids": ["g1"]},
            {"acl_external_user_group_ids": ["g1", "g2"]},
        )
        assert merged["acl_external_user_group_ids"] == ["g1", "g2"]

    def test_empty_lists_both_sides_yield_none(self):
        merged = union_acl(
            {"acl_external_user_emails": []},
            {"acl_external_user_emails": []},
        )
        assert merged["acl_external_user_emails"] is None

    def test_accumulation_across_batches(self):
        # 6.7: batch A then batch B accumulates into the union of both
        # (the ingestion pipeline folds each batch's document ACL this way).
        doc_a = {
            "acl_is_public": False,
            "acl_external_user_emails": ["a@x.com"],
            "acl_external_user_group_ids": None,
        }
        doc_b = {
            "acl_is_public": True,
            "acl_external_user_emails": ["b@x.com"],
            "acl_external_user_group_ids": ["grp-1"],
        }
        accumulated = None
        accumulated = union_acl(accumulated, doc_a)
        accumulated = union_acl(accumulated, doc_b)
        assert accumulated == {
            "acl_is_public": True,
            "acl_external_user_emails": ["a@x.com", "b@x.com"],
            "acl_external_user_group_ids": ["grp-1"],
        }


class TestGraphNodeAclSnapshot:
    def test_maps_graph_property_names_to_columns(self):
        node = {
            "is_public": True,
            "external_user_emails": ["a@x.com"],
            "external_user_group_ids": ["g1"],
        }
        assert graph_node_acl_snapshot(node) == {
            "acl_is_public": True,
            "acl_external_user_emails": ["a@x.com"],
            "acl_external_user_group_ids": ["g1"],
        }

    def test_missing_properties_yield_none(self):
        assert graph_node_acl_snapshot({"is_public": True}) == {
            "acl_is_public": True,
            "acl_external_user_emails": None,
            "acl_external_user_group_ids": None,
        }

    def test_none_node_yields_all_none(self):
        assert graph_node_acl_snapshot(None) == dict.fromkeys(ACL_FIELDS)


class TestExtractAcl:
    def test_with_external_access(self):
        text = (
            '{"external_access": {"is_public": true, '
            '"external_user_emails": ["a@x.com"], '
            '"external_user_group_ids": ["g1"]}}'
        )
        assert _extract_acl(text) == {
            "acl_is_public": True,
            "acl_external_user_emails": ["a@x.com"],
            "acl_external_user_group_ids": ["g1"],
        }

    def test_without_external_access_is_all_none(self):
        assert _extract_acl('{"title": "no acl"}') == {
            "acl_is_public": None,
            "acl_external_user_emails": None,
            "acl_external_user_group_ids": None,
        }

    def test_malformed_json_is_all_none(self):
        assert _extract_acl("not json at all")["acl_is_public"] is None
        assert _extract_acl("{broken")["acl_is_public"] is None

    def test_non_dict_payload_is_all_none(self):
        assert _extract_acl('["a", "b"]')["acl_is_public"] is None
        assert _extract_acl('"just a string"')["acl_is_public"] is None

    def test_external_access_not_a_dict_is_all_none(self):
        assert (
            _extract_acl('{"external_access": "yes"}')["acl_is_public"]
            is None
        )

    def test_empty_email_entries_dropped(self):
        text = (
            '{"external_access": {'
            '"external_user_emails": ["a@x.com", "", null]}}'
        )
        assert _extract_acl(text)["acl_external_user_emails"] == ["a@x.com"]
