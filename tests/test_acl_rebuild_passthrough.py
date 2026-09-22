"""Rebuild passthrough tests for the ACL seat snapshots (task 6.9).

The rebuild path rewrites entity VDB rows; the fork-custom payload must
carry the ACL fields (graph union properties, or all-None defaults) so a
rebuild never silently erases the seat snapshot the row already carried.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lightrag.operate import (
    _rebuild_single_entity,
    _rebuild_single_relationship,
)

pytestmark = pytest.mark.offline


def _global_config() -> dict:
    return {
        "max_source_ids_per_entity": 100,
        "max_source_ids_per_relation": 100,
        "max_file_paths": 10,
        "source_ids_limit_method": "KEEP",
        "file_path_more_placeholder": "more",
    }


class TestRebuildEntityCarriesAcl:
    @pytest.mark.asyncio
    async def test_carries_graph_union_properties(self):
        graph = MagicMock()
        graph.get_node = AsyncMock(
            return_value={
                "entity_id": "EntA",
                "description": "old",
                "entity_type": "X",
                "is_public": True,
                "external_user_emails": ["a@x.com"],
                "external_user_group_ids": ["grp-1"],
            }
        )
        graph.upsert_node = AsyncMock()
        entities_vdb = MagicMock()
        entities_vdb.upsert = AsyncMock()
        chunk_entities = {
            "chunk-1": {
                "EntA": [
                    {
                        "description": "new desc",
                        "entity_type": "X",
                        "file_path": "/f1",
                    }
                ]
            }
        }

        with patch(
            "lightrag.operate._handle_entity_relation_summary",
            new=AsyncMock(return_value=("final description", {})),
        ):
            await _rebuild_single_entity(
                knowledge_graph_inst=graph,
                entities_vdb=entities_vdb,
                entity_name="EntA",
                chunk_ids=["chunk-1"],
                chunk_entities=chunk_entities,
                llm_response_cache=MagicMock(),
                global_config=_global_config(),
            )

        entities_vdb.upsert.assert_awaited_once()
        vdb_data = entities_vdb.upsert.await_args.args[0]
        row = next(iter(vdb_data.values()))
        assert row["acl_is_public"] is True
        assert row["acl_external_user_emails"] == ["a@x.com"]
        assert row["acl_external_user_group_ids"] == ["grp-1"]

    @pytest.mark.asyncio
    async def test_node_without_acl_properties_yields_all_none(self):
        graph = MagicMock()
        graph.get_node = AsyncMock(
            return_value={
                "entity_id": "EntB",
                "description": "old",
                "entity_type": "X",
            }
        )
        graph.upsert_node = AsyncMock()
        entities_vdb = MagicMock()
        entities_vdb.upsert = AsyncMock()
        chunk_entities = {
            "chunk-1": {
                "EntB": [
                    {
                        "description": "new desc",
                        "entity_type": "X",
                        "file_path": "/f1",
                    }
                ]
            }
        }

        with patch(
            "lightrag.operate._handle_entity_relation_summary",
            new=AsyncMock(return_value=("final description", {})),
        ):
            await _rebuild_single_entity(
                knowledge_graph_inst=graph,
                entities_vdb=entities_vdb,
                entity_name="EntB",
                chunk_ids=["chunk-1"],
                chunk_entities=chunk_entities,
                llm_response_cache=MagicMock(),
                global_config=_global_config(),
            )

        vdb_data = entities_vdb.upsert.await_args.args[0]
        row = next(iter(vdb_data.values()))
        assert row["acl_is_public"] is None
        assert row["acl_external_user_emails"] is None
        assert row["acl_external_user_group_ids"] is None


class TestRebuildEndpointNodesCarryDefaults:
    @pytest.mark.asyncio
    async def test_missing_endpoint_node_rows_carry_all_none(self):
        # A relationship rebuild that has to create a missing endpoint
        # node writes a fresh entity row: the ACL fields must be present
        # with all-None defaults (fail-closed until the push recompute).
        graph = MagicMock()
        graph.get_node = AsyncMock(
            return_value={
                "src_id": "EntA",
                "tgt_id": "EntB",
                "description": "rel",
                "keywords": "",
                "weight": 1.0,
                "source_id": "chunk-1",
                "file_path": "/f1",
            }
        )
        graph.has_node = AsyncMock(return_value=False)
        graph.get_edge = AsyncMock(
            return_value={
                "description": "rel",
                "keywords": "k",
                "weight": 1.0,
                "source_id": "chunk-1",
                "file_path": "/f1",
            }
        )
        graph.upsert_node = AsyncMock()
        graph.upsert_edge = AsyncMock()
        entities_vdb = MagicMock()
        entities_vdb.upsert = AsyncMock()
        relationships_vdb = MagicMock()
        relationships_vdb.upsert = AsyncMock()
        chunk_relationships = {
            "chunk-1": {
                ("EntA", "EntB"): [
                    {
                        "description": "rel desc",
                        "keywords": "k",
                        "weight": 1.0,
                        "source_id": "chunk-1",
                        "file_path": "/f1",
                    }
                ]
            }
        }

        with patch(
            "lightrag.operate._handle_entity_relation_summary",
            new=AsyncMock(return_value=("rel final", {})),
        ):
            await _rebuild_single_relationship(
                knowledge_graph_inst=graph,
                relationships_vdb=relationships_vdb,
                entities_vdb=entities_vdb,
                src="EntA",
                tgt="EntB",
                chunk_ids=["chunk-1"],
                chunk_relationships=chunk_relationships,
                llm_response_cache=MagicMock(),
                global_config=_global_config(),
            )

        # The endpoint-node upsert carries the three ACL fields, all None.
        node_upserts = [
            call
            for call in entities_vdb.upsert.await_args_list
        ]
        assert node_upserts
        for call in node_upserts:
            row = next(iter(call.args[0].values()))
            assert row["acl_is_public"] is None
            assert row["acl_external_user_emails"] is None
            assert row["acl_external_user_group_ids"] is None
