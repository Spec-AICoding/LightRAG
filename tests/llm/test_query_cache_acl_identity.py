"""The requester ACL identity must partition the query-answer cache.

The ACL identity shapes the visible context and therefore the generated
answer, so it joined the answer-cache key (policy v3, fork-custom
query-acl): two different identities must never share an entry, the same
identity must hit its own, ``include_public`` counts as a difference, and
anonymous callers share one public-only entry. Both ``kg_query`` and
``naive_query`` compose the key, so both are pinned.
"""

import pytest

from lightrag.acl_identity import get_identity, reset_identity, set_identity
from lightrag.base import QueryContextResult, QueryParam
from lightrag.operate import kg_query, naive_query
from lightrag.utils import Tokenizer


class _FakeTokenizerImpl:
    def encode(self, content: str) -> list[int]:
        return [ord(ch) for ch in content]

    def decode(self, tokens: list[int]) -> str:
        return "".join(chr(token) for token in tokens)


def _FakeTokenizer() -> Tokenizer:
    return Tokenizer("fake", _FakeTokenizerImpl())


class _FakeKVStorage:
    def __init__(self):
        self.global_config = {"enable_llm_cache": True}
        self._store = {}

    async def get_by_id(self, key):
        return self._store.get(key)

    async def upsert(self, entries):
        self._store.update(entries)


class _FakeChunksVDB:
    cosine_better_than_threshold = 0.0

    async def query(self, *_args, **_kwargs):
        return [
            {
                "id": "chunk-1",
                "content": "ACL cache partitioning test chunk.",
                "file_path": "test.md",
            }
        ]


class _RecordingModel:
    """Counts calls; each call returns a distinct answer so an unexpected
    cache hit surfaces as a content/call-count mismatch."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, *_args, **_kwargs):
        self.calls += 1
        return f"answer-{self.calls}"


QUERY = "who is Tesla?"


def _query_global_config(llm_func) -> dict:
    return {
        "tokenizer": _FakeTokenizer(),
        "role_llm_funcs": {"query": llm_func},
        "addon_params": {"language": "en"},
        "min_rerank_score": 0.0,
        "max_total_tokens": 4096,
    }


def _answer_cache_keys(cache: _FakeKVStorage) -> list[str]:
    return [key for key in cache._store if ":query:" in key]


@pytest.fixture
def stub_query_context(monkeypatch):
    """Skip retrieval: kg_query only forwards its storage args into this call."""

    async def _fake_build_query_context(*_args, **_kwargs):
        return QueryContextResult(context="KG CONTEXT", raw_data={})

    monkeypatch.setattr(
        "lightrag.operate._build_query_context", _fake_build_query_context
    )


def _naive_param(**overrides) -> QueryParam:
    return QueryParam(mode="naive", enable_rerank=False, **overrides)


def _kg_param(**overrides) -> QueryParam:
    return QueryParam(
        mode="local", enable_rerank=False, ll_keywords=["Tesla"], **overrides
    )


async def _run_naive(param, cfg, cache):
    return await naive_query(
        QUERY, _FakeChunksVDB(), param, cfg, hashing_kv=cache
    )


async def _run_kg(param, cfg, cache):
    return await kg_query(
        QUERY, None, None, None, None, param, cfg, hashing_kv=cache
    )


@pytest.mark.offline
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner,param", [(_run_naive, _naive_param), (_run_kg, _kg_param)]
)
async def test_different_identities_do_not_share_entries(
    runner, param, stub_query_context
):
    cache = _FakeKVStorage()
    model = _RecordingModel()
    cfg = _query_global_config(model)

    token_a = set_identity(["a@x.com"])
    try:
        first = await runner(param(), cfg, cache)
    finally:
        reset_identity(token_a)

    token_b = set_identity(["b@x.com"])
    try:
        second = await runner(param(), cfg, cache)
    finally:
        reset_identity(token_b)

    assert get_identity() is None
    assert first.content == "answer-1"
    assert second.content == "answer-2"
    assert model.calls == 2
    assert len(_answer_cache_keys(cache)) == 2


@pytest.mark.offline
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner,param", [(_run_naive, _naive_param), (_run_kg, _kg_param)]
)
async def test_same_identity_hits_its_own_entry(runner, param, stub_query_context):
    cache = _FakeKVStorage()
    model = _RecordingModel()
    cfg = _query_global_config(model)

    token = set_identity(["a@x.com"])
    try:
        first = await runner(param(), cfg, cache)
        second = await runner(param(), cfg, cache)
    finally:
        reset_identity(token)

    assert first.content == second.content == "answer-1"
    assert model.calls == 1
    assert len(_answer_cache_keys(cache)) == 1


@pytest.mark.offline
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner,param", [(_run_naive, _naive_param), (_run_kg, _kg_param)]
)
async def test_include_public_partitions_the_cache(
    runner, param, stub_query_context
):
    cache = _FakeKVStorage()
    model = _RecordingModel()
    cfg = _query_global_config(model)

    token_a = set_identity(["a@x.com"], include_public=True)
    try:
        first = await runner(param(), cfg, cache)
    finally:
        reset_identity(token_a)

    token_b = set_identity(["a@x.com"], include_public=False)
    try:
        second = await runner(param(), cfg, cache)
    finally:
        reset_identity(token_b)

    assert first.content == "answer-1"
    assert second.content == "answer-2"
    assert model.calls == 2


@pytest.mark.offline
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner,param", [(_run_naive, _naive_param), (_run_kg, _kg_param)]
)
async def test_anonymous_and_identified_do_not_share_entries(
    runner, param, stub_query_context
):
    cache = _FakeKVStorage()
    model = _RecordingModel()
    cfg = _query_global_config(model)

    first = await runner(param(), cfg, cache)

    token = set_identity(["a@x.com"])
    try:
        second = await runner(param(), cfg, cache)
    finally:
        reset_identity(token)

    assert first.content == "answer-1"
    assert second.content == "answer-2"
    assert model.calls == 2
    assert len(_answer_cache_keys(cache)) == 2


@pytest.mark.offline
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner,param", [(_run_naive, _naive_param), (_run_kg, _kg_param)]
)
async def test_anonymous_callers_share_one_entry(runner, param, stub_query_context):
    cache = _FakeKVStorage()
    model = _RecordingModel()
    cfg = _query_global_config(model)

    first = await runner(param(), cfg, cache)
    second = await runner(param(), cfg, cache)

    assert first.content == second.content == "answer-1"
    assert model.calls == 1
    assert len(_answer_cache_keys(cache)) == 1
