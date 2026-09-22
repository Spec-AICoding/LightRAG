"""Tests for the ACL filter hook (tasks 6.4/6.5).

Covers the ② chunk-lineage collapse judgment and the ③ entity/relation
authority judgment: visibility is decided by the chunks-VDB visible-id
query plus the in-memory graph union properties, and every absent id /
property means invisible (fail-closed).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from lightrag.acl_filter import (
    _entity_visible,
    _relation_visible,
    apply_acl_filter,
)
from lightrag.acl_identity import ACLIdentity, set_identity, reset_identity

pytestmark = pytest.mark.offline


def _truncation_result():
    return {
        "entities_context": [
            {"entity": "EntA"},
            {"entity": "EntB"},
            {"entity": "EntC"},
        ],
        "filtered_entities": [
            {"entity_name": "EntA", "is_public": True},
            {
                "entity_name": "EntB",
                "is_public": False,
                "external_user_emails": ["b@x.com"],
            },
            {
                "entity_name": "EntC",
                "is_public": False,
                "external_user_group_ids": ["grp-1"],
            },
        ],
        "entity_id_to_original": {
            "EntA": {"entity_name": "EntA"},
            "EntB": {"entity_name": "EntB"},
            "EntC": {"entity_name": "EntC"},
        },
        "relations_context": [
            {"entity1": "EntA", "entity2": "EntB"},
            {"entity1": "EntB", "entity2": "EntC"},
        ],
        "filtered_relations": [
            # First entry uses the graph's production shape (<SEP>-joined
            # string), the second the list form, so the full apply path
            # covers both.
            {"src_id": "EntA", "tgt_id": "EntB", "source_id": "chunk-1"},
            {"src_id": "EntB", "tgt_id": "EntC", "source_id": ["chunk-9"]},
        ],
        "relation_id_to_original": {
            ("EntA", "EntB"): {"src_id": "EntA", "tgt_id": "EntB"},
            ("EntB", "EntC"): {"src_id": "EntB", "tgt_id": "EntC"},
        },
    }


class TestEntityVisible:
    def test_public_entity_visible_even_without_identity(self):
        assert _entity_visible({"is_public": True}, None)

    def test_no_identity_hides_non_public(self):
        assert not _entity_visible(
            {"is_public": False, "external_user_emails": ["a@x.com"]}, None
        )

    def test_email_intersection_visible(self):
        entity = {"external_user_emails": ["a@x.com", "z@x.com"]}
        identity = ACLIdentity(emails=("a@x.com",))
        assert _entity_visible(entity, identity)

    def test_group_intersection_visible(self):
        entity = {"external_user_group_ids": ["grp-9"]}
        identity = ACLIdentity(group_ids=("grp-9",))
        assert _entity_visible(entity, identity)

    def test_disjoint_identity_invisible(self):
        entity = {
            "external_user_emails": ["a@x.com"],
            "external_user_group_ids": ["grp-1"],
        }
        identity = ACLIdentity(
            emails=("b@x.com",), group_ids=("grp-2",)
        )
        assert not _entity_visible(entity, identity)

    def test_missing_properties_invisible(self):
        assert not _entity_visible({}, ACLIdentity(emails=("a@x.com",)))

    def test_public_hidden_when_include_public_false_without_share(self):
        entity = {"is_public": True}
        identity = ACLIdentity(emails=("a@x.com",), include_public=False)
        assert not _entity_visible(entity, identity)

    def test_public_hidden_when_include_public_false_without_identity(self):
        entity = {"is_public": True}
        identity = ACLIdentity(include_public=False)
        assert not _entity_visible(entity, identity)

    def test_explicit_share_still_visible_when_include_public_false(self):
        entity = {"is_public": True, "external_user_emails": ["a@x.com"]}
        identity = ACLIdentity(emails=("a@x.com",), include_public=False)
        assert _entity_visible(entity, identity)


class TestRelationVisible:
    def test_any_lineage_chunk_visible(self):
        assert _relation_visible({"source_id": ["c1", "c2"]}, {"c2"})

    def test_all_lineage_invisible(self):
        assert not _relation_visible({"source_id": ["c1"]}, {"c2"})

    def test_no_source_id_invisible(self):
        assert not _relation_visible({}, {"c1"})
        assert not _relation_visible({"source_id": []}, {"c1"})

    # The graph stores source_id as a <SEP>-joined string (or a bare single
    # chunk id) — the production shape, unlike the list fixtures above.
    def test_sep_joined_string_any_chunk_visible(self):
        assert _relation_visible(
            {"source_id": "chunk-a<SEP>chunk-b"}, {"chunk-b"}
        )

    def test_bare_single_chunk_id_string_visible(self):
        assert _relation_visible({"source_id": "chunk-a"}, {"chunk-a"})

    def test_sep_joined_string_all_invisible(self):
        assert not _relation_visible(
            {"source_id": "chunk-a<SEP>chunk-b"}, {"chunk-c"}
        )

    def test_empty_string_source_id_invisible(self):
        assert not _relation_visible({"source_id": ""}, {"chunk-a"})


class TestApplyAclFilter:
    @pytest.mark.asyncio
    async def test_collapse_and_authority_filtering(self):
        # Identity b@x.com: EntB visible by email; chunk-1 visible.
        token = set_identity(["b@x.com"])
        try:
            chunks_vdb = MagicMock()
            chunks_vdb.query_acl_visible_ids = AsyncMock(
                return_value={"chunk-1"}
            )
            merged_chunks = [
                {"chunk_id": "chunk-1"},
                {"chunk_id": "chunk-9"},
            ]
            truncation_result = _truncation_result()

            result, chunks = await apply_acl_filter(
                truncation_result, merged_chunks, chunks_vdb
            )

            # ② merged chunks reduced to the visible subset.
            assert [c["chunk_id"] for c in chunks] == ["chunk-1"]

            # ③ entities: EntA (public), EntB (email); EntC hidden.
            assert [e["entity"] for e in result["entities_context"]] == [
                "EntA",
                "EntB",
            ]
            assert [e["entity_name"] for e in result["filtered_entities"]] == [
                "EntA",
                "EntB",
            ]
            assert set(result["entity_id_to_original"]) == {"EntA", "EntB"}

            # Relations: EntA-EntB lineage chunk-1 visible; EntB-EntC
            # lineage chunk-9 hidden.
            assert result["relations_context"] == [
                {"entity1": "EntA", "entity2": "EntB"}
            ]
            assert [
                (r["src_id"], r["tgt_id"])
                for r in result["filtered_relations"]
            ] == [("EntA", "EntB")]
            assert set(result["relation_id_to_original"]) == {
                ("EntA", "EntB")
            }
        finally:
            reset_identity(token)

    @pytest.mark.asyncio
    async def test_chunk_id_absent_from_milvus_is_invisible(self):
        # ② fail-closed: a chunk whose id the visible query never returns
        # is dropped even when it was in merged_chunks.
        token = set_identity(["b@x.com"])
        try:
            chunks_vdb = MagicMock()
            chunks_vdb.query_acl_visible_ids = AsyncMock(return_value=set())
            merged_chunks = [{"chunk_id": "chunk-ghost"}]
            truncation_result = {
                "filtered_entities": [],
                "filtered_relations": [],
                "entities_context": [],
                "relations_context": [],
                "entity_id_to_original": {},
                "relation_id_to_original": {},
            }

            _, chunks = await apply_acl_filter(
                truncation_result, merged_chunks, chunks_vdb
            )
            assert chunks == []
        finally:
            reset_identity(token)

    @pytest.mark.asyncio
    async def test_relation_without_lineage_dropped(self):
        # ③ fail-closed: a relation with no source_id has no visibility
        # evidence and is dropped even when its entities are visible.
        token = set_identity(["b@x.com"])
        try:
            chunks_vdb = MagicMock()
            chunks_vdb.query_acl_visible_ids = AsyncMock(
                return_value={"chunk-1"}
            )
            truncation_result = {
                "filtered_entities": [],
                "filtered_relations": [
                    {"src_id": "EntA", "tgt_id": "EntB", "source_id": []}
                ],
                "entities_context": [],
                "relations_context": [
                    {"entity1": "EntA", "entity2": "EntB"}
                ],
                "entity_id_to_original": {},
                "relation_id_to_original": {
                    ("EntA", "EntB"): {"src_id": "EntA", "tgt_id": "EntB"}
                },
            }

            result, _ = await apply_acl_filter(
                truncation_result, [], chunks_vdb
            )
            assert result["filtered_relations"] == []
            assert result["relations_context"] == []
            assert result["relation_id_to_original"] == {}
        finally:
            reset_identity(token)

    @pytest.mark.asyncio
    async def test_missing_query_method_is_fail_closed(self):
        # A non-Milvus chunks backend has no query_acl_visible_ids: keep
        # nothing rather than everything.
        token = set_identity(["b@x.com"])
        try:
            # No query_acl_visible_ids attribute at all (explicitly None so
            # MagicMock does not fabricate an awaitable): keep nothing.
            chunks_vdb = MagicMock()
            chunks_vdb.query_acl_visible_ids = None
            merged_chunks = [{"chunk_id": "chunk-1"}]
            truncation_result = _truncation_result()

            result, chunks = await apply_acl_filter(
                truncation_result, merged_chunks, chunks_vdb
            )
            assert chunks == []
            assert result["filtered_relations"] == []
        finally:
            reset_identity(token)

    @pytest.mark.asyncio
    async def test_entity_union_visible_when_stale_seat_would_hide(self):
        # 6.3 stale-snapshot direction: the seat expr is not simulated
        # here; the hook re-judges from the graph union properties only,
        # so a public entity stays visible regardless of row state.
        token = set_identity(["b@x.com"])
        try:
            chunks_vdb = MagicMock()
            chunks_vdb.query_acl_visible_ids = AsyncMock(
                return_value={"chunk-1"}
            )
            truncation_result = {
                "filtered_entities": [
                    {"entity_name": "EntA", "is_public": True},
                    {"entity_name": "EntB", "is_public": False},
                ],
                "filtered_relations": [],
                "entities_context": [
                    {"entity": "EntA"},
                    {"entity": "EntB"},
                ],
                "relations_context": [],
                "entity_id_to_original": {
                    "EntA": {"entity_name": "EntA"},
                    "EntB": {"entity_name": "EntB"},
                },
                "relation_id_to_original": {},
            }

            result, _ = await apply_acl_filter(
                truncation_result, [], chunks_vdb
            )
            assert [e["entity"] for e in result["entities_context"]] == [
                "EntA"
            ]
        finally:
            reset_identity(token)
