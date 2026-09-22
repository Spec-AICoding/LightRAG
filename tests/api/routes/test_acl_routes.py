"""ACL endpoint tests (tasks 6.6/6.8).

6.6: the three query endpoints accept ``user_emails`` / ``user_group_ids``,
exclude them from ``QueryParam``, set the identity ContextVar for the query
duration (inherited by the stream task), and reset it afterwards.
6.8: the internal ``/acl/chunks`` and ``/acl/entities`` endpoints update
seat snapshots via the fork-custom VDB methods, fail loud when the backend
lacks them, and are auth-protected (dependency present on the routes).
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from lightrag.acl_identity import get_identity
from lightrag.utils import compute_mdhash_id

_ENV_VARS_TO_ISOLATE = (
    "LLM_BINDING",
    "EMBEDDING_BINDING",
    "LLM_BINDING_HOST",
    "LLM_BINDING_API_KEY",
    "LLM_MODEL",
    "EMBEDDING_BINDING_HOST",
    "EMBEDDING_BINDING_API_KEY",
    "EMBEDDING_MODEL",
    "LIGHTRAG_API_PREFIX",
    "LIGHTRAG_KV_STORAGE",
    "LIGHTRAG_VECTOR_STORAGE",
    "LIGHTRAG_GRAPH_STORAGE",
    "LIGHTRAG_DOC_STATUS_STORAGE",
    "AUTH_ACCOUNTS",
    "TOKEN_SECRET",
    "WHITELIST_PATHS",
    "LIGHTRAG_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in _ENV_VARS_TO_ISOLATE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_BINDING", "ollama")
    monkeypatch.setenv("EMBEDDING_BINDING", "ollama")
    monkeypatch.setenv("AUTH_ACCOUNTS", "")
    monkeypatch.setenv("TOKEN_SECRET", "")
    monkeypatch.setenv("LIGHTRAG_API_KEY", "")

    auth_module = sys.modules.get("lightrag.api.auth")
    if auth_module is not None:
        monkeypatch.setattr(auth_module.auth_handler, "accounts", {}, raising=False)
    utils_api_module = sys.modules.get("lightrag.api.utils_api")
    if utils_api_module is not None:
        monkeypatch.setattr(utils_api_module, "auth_configured", False, raising=False)


def _build_client(captured: dict):
    original_argv = sys.argv.copy()
    try:
        sys.argv = ["lightrag-server"]
        from lightrag.api.config import parse_args
        from lightrag.api.lightrag_server import create_app

        args = parse_args()

        async def fake_aquery_llm(query, param=None, **kwargs):
            captured["identity"] = get_identity()
            captured["top_k"] = getattr(param, "top_k", None)
            captured["param_fields"] = (
                set(param.__dict__.keys()) if param is not None else set()
            )
            return {
                "llm_response": {
                    "content": "ok",
                    "llm_generated": True,
                    "references": [],
                },
                "data": {},
            }

        with patch("lightrag.api.lightrag_server.LightRAG") as mock_rag:
            mock_rag.return_value = MagicMock()
            mock_rag.return_value.aquery_llm = AsyncMock(
                side_effect=fake_aquery_llm
            )
            return TestClient(create_app(args))
    finally:
        sys.argv = original_argv


class TestQueryIdentityFields:
    def test_query_sets_identity_for_duration(self):
        captured: dict = {}
        client = _build_client(captured)
        response = client.post(
            "/query",
            json={
                "query": "What is the secret project?",
                "mode": "mix",
                "user_emails": ["a@x.com"],
                "user_group_ids": ["grp-1"],
            },
        )
        assert response.status_code == 200
        assert captured["identity"].emails == ("a@x.com",)
        assert captured["identity"].group_ids == ("grp-1",)
        # Reset after the request completes.
        assert get_identity() is None

    def test_query_without_identity_is_none(self):
        captured: dict = {}
        client = _build_client(captured)
        response = client.post(
            "/query", json={"query": "What is the public project?", "mode": "mix"}
        )
        assert response.status_code == 200
        assert captured["identity"] is None

    def test_identity_fields_excluded_from_query_param(self):
        captured: dict = {}
        client = _build_client(captured)
        response = client.post(
            "/query",
            json={
                "query": "What is the secret project?",
                "mode": "mix",
                "user_emails": ["a@x.com"],
                "user_group_ids": ["grp-1"],
                "include_public": False,
            },
        )
        assert response.status_code == 200
        assert "user_emails" not in captured["param_fields"]
        assert "user_group_ids" not in captured["param_fields"]
        assert "include_public" not in captured["param_fields"]

    def test_include_public_defaults_to_true(self):
        captured: dict = {}
        client = _build_client(captured)
        response = client.post(
            "/query",
            json={
                "query": "What is the secret project?",
                "mode": "mix",
                "user_emails": ["a@x.com"],
            },
        )
        assert response.status_code == 200
        assert captured["identity"].include_public is True

    def test_include_public_false_without_identity_is_no_error(self):
        # The caller opted out of public docs and carried no identity: the
        # request must succeed (200) and hand an identity that excludes the
        # public clause — the empty result set is the retrieval layer's
        # fail-closed answer, not an HTTP error.
        captured: dict = {}
        client = _build_client(captured)
        response = client.post(
            "/query",
            json={
                "query": "What is the public project?",
                "mode": "mix",
                "include_public": False,
            },
        )
        assert response.status_code == 200
        assert captured["identity"] is not None
        assert captured["identity"].include_public is False
        assert captured["identity"].emails == ()
        assert captured["identity"].group_ids == ()

    def test_top_k_headroom_expansion(self):
        captured: dict = {}
        client = _build_client(captured)
        response = client.post(
            "/query",
            json={
                "query": "What is the secret project?",
                "mode": "mix",
                "top_k": 10,
                "user_emails": ["a@x.com"],
            },
        )
        assert response.status_code == 200
        assert captured["top_k"] == 20

    def test_top_k_unchanged_without_identity(self):
        captured: dict = {}
        client = _build_client(captured)
        response = client.post(
            "/query",
            json={"query": "What is the public project?", "mode": "mix", "top_k": 10},
        )
        assert response.status_code == 200
        assert captured["top_k"] == 10

    def test_stream_endpoint_inherits_identity_in_task(self):
        captured: dict = {}
        client = _build_client(captured)
        with client.stream(
            "POST",
            "/query/stream",
            json={
                "query": "What is the secret project?",
                "mode": "mix",
                "user_emails": ["a@x.com"],
            },
        ) as response:
            assert response.status_code == 200
            content = "".join(response.iter_text())
        assert captured["identity"].emails == ("a@x.com",)
        # The stream task ran inside the request context; after the request
        # completes the identity is reset.
        assert get_identity() is None


def _make_acl_client(rag: MagicMock) -> TestClient:
    # Imported lazily: the module chain reaches lightrag.api.auth, whose
    # module-level AuthHandler parses sys.argv — safe only inside a test.
    from lightrag.api.routers.acl_routes import create_acl_routes
    from fastapi import FastAPI

    router = create_acl_routes(rag, api_key=None)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestChunkAclEndpoint:
    def test_updates_chunk_rows_by_biz_doc_id(self):
        rag = MagicMock()
        rag.chunks_vdb.query_ids_by_biz_doc_id = AsyncMock(
            return_value=["c1", "c2"]
        )
        rag.chunks_vdb.upsert_acl_fields = AsyncMock()
        client = _make_acl_client(rag)

        response = client.post(
            "/acl/chunks",
            json={
                "biz_doc_id": "doc-1",
                "is_public": True,
                "emails": ["a@x.com"],
                "groups": ["grp-1"],
            },
        )
        assert response.status_code == 200
        assert response.json() == {"updated": 2}
        rag.chunks_vdb.upsert_acl_fields.assert_awaited_once()
        data = rag.chunks_vdb.upsert_acl_fields.await_args.args[0]
        assert set(data) == {"c1", "c2"}
        assert data["c1"] == {
            "acl_is_public": True,
            "acl_external_user_emails": ["a@x.com"],
            "acl_external_user_group_ids": ["grp-1"],
        }

    def test_no_matching_chunks_updates_nothing(self):
        rag = MagicMock()
        rag.chunks_vdb.query_ids_by_biz_doc_id = AsyncMock(return_value=[])
        rag.chunks_vdb.upsert_acl_fields = AsyncMock()
        client = _make_acl_client(rag)

        response = client.post(
            "/acl/chunks",
            json={"biz_doc_id": "doc-1", "is_public": False},
        )
        assert response.status_code == 200
        assert response.json() == {"updated": 0}
        rag.chunks_vdb.upsert_acl_fields.assert_not_awaited()

    def test_non_milvus_backend_is_501(self):
        rag = MagicMock()
        rag.chunks_vdb = MagicMock()
        rag.chunks_vdb.query_ids_by_biz_doc_id = None
        rag.chunks_vdb.upsert_acl_fields = None
        client = _make_acl_client(rag)

        response = client.post(
            "/acl/chunks", json={"biz_doc_id": "doc-1", "is_public": False}
        )
        assert response.status_code == 501


class TestEntityAclEndpoint:
    def test_recompute_chain_merges_and_upserts(self):
        rag = MagicMock()
        rag.chunks_vdb.query_acl_snapshots_by_biz_doc_ids = AsyncMock(
            return_value={
                "doc-1": {
                    "acl_is_public": False,
                    "acl_external_user_emails": ["a@x.com"],
                    "acl_external_user_group_ids": None,
                },
                "doc-2": {
                    "acl_is_public": True,
                    "acl_external_user_emails": ["b@x.com"],
                    "acl_external_user_group_ids": ["grp-1"],
                },
            }
        )
        rag.entities_vdb.upsert_acl_fields = AsyncMock()
        client = _make_acl_client(rag)

        with patch(
            "lightrag.api.routers.acl_routes._reverse_lookup_entities",
            new=AsyncMock(
                return_value={"王峰": ["doc-1", "doc-2"], "HP-11": ["doc-1"]}
            ),
        ):
            response = client.post(
                "/acl/entities", json={"biz_doc_id": "doc-1"}
            )

        assert response.status_code == 200
        assert response.json() == {"entities": 2}
        data = rag.entities_vdb.upsert_acl_fields.await_args.args[0]
        # Regression: the graph keys entities by NAME; the upsert keys must
        # be the row primary keys ent-<md5(name)>, never the raw name.
        assert "王峰" not in data
        assert "HP-11" not in data
        assert data[compute_mdhash_id("王峰", prefix="ent-")] == {
            "acl_is_public": True,
            "acl_external_user_emails": ["a@x.com", "b@x.com"],
            "acl_external_user_group_ids": ["grp-1"],
        }
        assert data[compute_mdhash_id("HP-11", prefix="ent-")] == {
            "acl_is_public": False,
            "acl_external_user_emails": ["a@x.com"],
            "acl_external_user_group_ids": None,
        }

    def test_no_entities_is_noop(self):
        rag = MagicMock()
        rag.entities_vdb.upsert_acl_fields = AsyncMock()
        client = _make_acl_client(rag)

        with patch(
            "lightrag.api.routers.acl_routes._reverse_lookup_entities",
            new=AsyncMock(return_value={}),
        ):
            response = client.post(
                "/acl/entities", json={"biz_doc_id": "doc-1"}
            )

        assert response.status_code == 200
        assert response.json() == {"entities": 0}
        rag.entities_vdb.upsert_acl_fields.assert_not_awaited()

    def test_non_milvus_backend_is_501(self):
        rag = MagicMock()
        rag.chunks_vdb = MagicMock()
        rag.chunks_vdb.query_acl_snapshots_by_biz_doc_ids = None
        client = _make_acl_client(rag)

        with patch(
            "lightrag.api.routers.acl_routes._reverse_lookup_entities",
            new=AsyncMock(return_value={"王峰": ["doc-1"]}),
        ):
            response = client.post(
                "/acl/entities", json={"biz_doc_id": "doc-1"}
            )
        assert response.status_code == 501

    def test_routes_carry_auth_dependency(self):
        from lightrag.api.routers.acl_routes import create_acl_routes

        rag = MagicMock()
        router = create_acl_routes(rag, api_key=None)
        for route in router.routes:
            if route.path in ("/acl/chunks", "/acl/entities"):
                assert len(route.dependencies) == 1, (
                    f"{route.path} must be auth-protected"
                )
