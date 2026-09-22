"""Milvus ACL seat-filter and ACL-update helper tests (tasks 6.2/6.3/6.8-half).

Offline: the MilvusClient is mocked, so these pin the contract between the
fork-custom call sites and the SDK — which filter reaches ``search`` /
``query`` / ``upsert``, when it is absent, and how ids and ACL lists are
bounded.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lightrag.acl_identity import reset_identity, set_identity
from lightrag.kg.milvus_impl import (
    ACL_ARRAY_ELEMENT_MAX_LENGTH,
    ACL_ARRAY_MAX_CAPACITY,
    MilvusVectorDBStorage,
    _cap_acl_list,
)
from lightrag.kg.shared_storage import finalize_share_data, initialize_share_data

pytestmark = pytest.mark.offline


@pytest.fixture(autouse=True)
def setup_shared_data():
    initialize_share_data()
    yield
    finalize_share_data()


def _make_storage(
    *,
    namespace: str = "test_chunks",
    meta_fields: set[str] | None = None,
) -> MilvusVectorDBStorage:
    mock_embedding_func = MagicMock()
    mock_embedding_func.embedding_dim = 128
    storage = MilvusVectorDBStorage(
        namespace=namespace,
        workspace="test_workspace",
        global_config={
            "embedding_batch_num": 100,
            "vector_db_storage_cls_kwargs": {
                "cosine_better_than_threshold": 0.3,
            },
        },
        embedding_func=mock_embedding_func,
        meta_fields=meta_fields
        or {
            "content",
            "biz_doc_id",
            "acl_is_public",
            "acl_external_user_emails",
            "acl_external_user_group_ids",
        },
    )
    storage._client = MagicMock()
    storage._ensure_collection_loaded = MagicMock()
    return storage


def _patch_executor(storage, search_results=None, query_results=None):
    """Patch run_in_milvus_executor; calls resolve by client-method identity."""
    if search_results is None:
        search_results = []
    if query_results is None:
        query_results = []

    def fake_executor(fn, *args, **kwargs):
        if fn is storage._client.search:
            return search_results
        if fn is storage._client.query:
            return query_results
        if fn is storage._client.upsert:
            return {"upsert_count": 0}
        return None

    mock_executor = AsyncMock(side_effect=fake_executor)
    return patch(
        "lightrag.kg.milvus_impl.run_in_milvus_executor", mock_executor
    )


class TestQuerySeatFilter:
    @pytest.mark.asyncio
    async def test_identity_produces_full_expr(self):
        storage = _make_storage()
        search_results = [[{"entity": {"content": "x"}, "id": "c1", "distance": 0.1}]]
        token = set_identity(["a@x.com"], ["grp-1"])
        try:
            with _patch_executor(storage, search_results=search_results) as mock_exec:
                await storage.query(
                    "q", top_k=5, query_embedding=[0.1] * 128
                )
            _, search_kwargs = mock_exec.await_args_list[1]
            expected = (
                '(acl_is_public == true) or '
                '(ARRAY_CONTAINS_ANY(acl_external_user_emails, ["a@x.com"])) or '
                '(ARRAY_CONTAINS_ANY(acl_external_user_group_ids, ["grp-1"]))'
            )
            assert search_kwargs["filter"] == expected
        finally:
            reset_identity(token)

    @pytest.mark.asyncio
    async def test_no_identity_is_public_only(self):
        storage = _make_storage()
        search_results = [[{"entity": {"content": "x"}, "id": "c1", "distance": 0.1}]]
        with _patch_executor(storage, search_results=search_results) as mock_exec:
            await storage.query("q", top_k=5, query_embedding=[0.1] * 128)
        _, search_kwargs = mock_exec.await_args_list[1]
        assert search_kwargs["filter"] == "(acl_is_public == true)"

    @pytest.mark.asyncio
    async def test_relationships_namespace_gets_no_filter(self):
        # 6.2 guard: the relationships collection has no ACL columns and
        # must never receive a filter referencing them.
        storage = _make_storage(
            namespace="test_relationships",
            meta_fields={"content", "src_id", "tgt_id"},
        )
        search_results = [[{"entity": {"content": "x"}, "id": "r1", "distance": 0.1}]]
        with _patch_executor(storage, search_results=search_results) as mock_exec:
            await storage.query("q", top_k=5, query_embedding=[0.1] * 128)
        _, search_kwargs = mock_exec.await_args_list[1]
        assert search_kwargs["filter"] is None

    @pytest.mark.asyncio
    async def test_entities_namespace_gets_expr(self):
        # 6.3 seat filter: the entities collection carries the union
        # snapshot columns and gets the same expr.
        storage = _make_storage(
            namespace="test_entities",
            meta_fields={
                "entity_name",
                "acl_is_public",
                "acl_external_user_emails",
                "acl_external_user_group_ids",
            },
        )
        search_results = [[{"entity": {"content": "x"}, "id": "e1", "distance": 0.1}]]
        token = set_identity([], ["grp-9"])
        try:
            with _patch_executor(storage, search_results=search_results) as mock_exec:
                await storage.query("q", top_k=5, query_embedding=[0.1] * 128)
            _, search_kwargs = mock_exec.await_args_list[1]
            assert (
                'ARRAY_CONTAINS_ANY(acl_external_user_group_ids, ["grp-9"])'
                in search_kwargs["filter"]
            )
        finally:
            reset_identity(token)


class TestQueryAclVisibleIds:
    @pytest.mark.asyncio
    async def test_combines_id_in_and_expr(self):
        storage = _make_storage()
        token = set_identity(["a@x.com"])
        try:
            with _patch_executor(storage, query_results=[{"id": "c1"}]) as mock_exec:
                visible = await storage.query_acl_visible_ids(["c1", "c2"])
            assert visible == ["c1"]
            _, query_kwargs = mock_exec.await_args_list[0]
            assert query_kwargs["filter"] == (
                '(id in ["c1", "c2"]) and '
                '((acl_is_public == true) or '
                '(ARRAY_CONTAINS_ANY(acl_external_user_emails, ["a@x.com"])))'
            )
            assert query_kwargs["output_fields"] == ["id"]
        finally:
            reset_identity(token)

    @pytest.mark.asyncio
    async def test_empty_ids_skip_query(self):
        storage = _make_storage()
        with _patch_executor(storage) as mock_exec:
            visible = await storage.query_acl_visible_ids([])
        assert visible == []
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_batches_are_bounded_at_256(self):
        storage = _make_storage()
        ids = [f"c{i}" for i in range(300)]
        with _patch_executor(storage, query_results=[]) as mock_exec:
            await storage.query_acl_visible_ids(ids)
        assert len(mock_exec.await_args_list) == 2
        first = mock_exec.await_args_list[0].kwargs["filter"]
        second = mock_exec.await_args_list[1].kwargs["filter"]
        assert "c255" in first
        assert "c256" in second
        assert "c256" not in first


class TestQueryIdsByBizDocId:
    @pytest.mark.asyncio
    async def test_escapes_and_returns_ids(self):
        storage = _make_storage()
        with _patch_executor(
            storage, query_results=[{"id": "c1"}, {"id": "c2"}]
        ) as mock_exec:
            ids = await storage.query_ids_by_biz_doc_id('doc"1')
        assert ids == ["c1", "c2"]
        _, query_kwargs = mock_exec.await_args_list[0]
        assert query_kwargs["filter"] == 'biz_doc_id == "doc\\"1"'


class TestQueryAclSnapshotsByBizDocIds:
    @pytest.mark.asyncio
    async def test_first_row_per_document_wins(self):
        storage = _make_storage()
        rows = [
            {
                "biz_doc_id": "doc-a",
                "acl_is_public": False,
                "acl_external_user_emails": ["a@x.com"],
                "acl_external_user_group_ids": None,
            },
            {
                "biz_doc_id": "doc-a",
                "acl_is_public": True,  # same-document rows share the snapshot
                "acl_external_user_emails": ["other@x.com"],
                "acl_external_user_group_ids": None,
            },
            {
                "biz_doc_id": "doc-b",
                "acl_is_public": True,
                "acl_external_user_emails": None,
                "acl_external_user_group_ids": ["g1"],
            },
        ]
        with _patch_executor(storage, query_results=rows):
            snapshots = await storage.query_acl_snapshots_by_biz_doc_ids(
                ["doc-a", "doc-b"]
            )
        assert snapshots == {
            "doc-a": {
                "acl_is_public": False,
                "acl_external_user_emails": ["a@x.com"],
                "acl_external_user_group_ids": None,
            },
            "doc-b": {
                "acl_is_public": True,
                "acl_external_user_emails": None,
                "acl_external_user_group_ids": ["g1"],
            },
        }

    @pytest.mark.asyncio
    async def test_rows_without_biz_doc_id_skipped(self):
        storage = _make_storage()
        with _patch_executor(
            storage, query_results=[{"acl_is_public": True}]
        ):
            snapshots = await storage.query_acl_snapshots_by_biz_doc_ids(
                ["doc-a"]
            )
        assert snapshots == {}


class TestUpsertAclFields:
    @pytest.mark.asyncio
    async def test_only_acl_fields_are_written(self):
        storage = _make_storage()
        with _patch_executor(storage) as mock_exec:
            await storage.upsert_acl_fields(
                {
                    "c1": {
                        "acl_is_public": True,
                        "acl_external_user_emails": ["a@x.com"],
                        "acl_external_user_group_ids": [],
                    }
                }
            )
        _, upsert_kwargs = mock_exec.await_args_list[0]
        # Without partial_update=True the client rejects ACL-only rows.
        assert upsert_kwargs["partial_update"] is True
        assert upsert_kwargs["data"] == [
            {
                "id": "c1",
                "acl_is_public": True,
                "acl_external_user_emails": ["a@x.com"],
                "acl_external_user_group_ids": [],
            }
        ]

    @pytest.mark.asyncio
    async def test_empty_data_is_noop(self):
        storage = _make_storage()
        with _patch_executor(storage) as mock_exec:
            await storage.upsert_acl_fields({})
        mock_exec.assert_not_awaited()


class TestCapAclList:
    def test_dedup_preserving_order(self):
        assert _cap_acl_list(["a", "b", "a"], "c1", "emails") == ["a", "b"]

    def test_drops_empty_entries(self):
        assert _cap_acl_list(["a", "", None, "b"], "c1", "emails") == [
            "a",
            "b",
        ]

    def test_truncates_oversized_elements(self):
        long_value = "x" * (ACL_ARRAY_ELEMENT_MAX_LENGTH + 10)
        result = _cap_acl_list([long_value], "c1", "emails")
        assert result == ["x" * ACL_ARRAY_ELEMENT_MAX_LENGTH]

    def test_caps_list_at_schema_capacity(self):
        values = [f"e{i}" for i in range(ACL_ARRAY_MAX_CAPACITY + 5)]
        result = _cap_acl_list(values, "c1", "emails")
        assert len(result) == ACL_ARRAY_MAX_CAPACITY

    def test_none_input_yields_empty(self):
        assert _cap_acl_list(None, "c1", "emails") == []
