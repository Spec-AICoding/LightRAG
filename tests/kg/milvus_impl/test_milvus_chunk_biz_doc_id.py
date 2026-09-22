"""Sentinel tests for the fork-custom ``biz_doc_id`` chunk field.

Guards the two halves of the "merge-clean contract" (see
openspec/changes/add-chunk-biz-doc-id): a future upstream merge that silently
drops either half must fail these tests loudly, because the production
failure mode without them is a silent field drop:

1. the Milvus schema manifests — schema columns, varchar limits, the
   auto-migration trigger list, the required-fields report, and the
   over-length identity guard;
2. the LightRAG-side ``chunks_vdb`` ``meta_fields`` whitelist — without it
   the Milvus writer silently discards the field even when the ingestion
   dict carries it.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import numpy as np
import pytest
from pymilvus import DataType

from lightrag import LightRAG
from lightrag.kg.milvus_impl import (
    MILVUS_IDENTITY_VARCHAR_FIELDS,
    MILVUS_SEPARATOR_JOINED_FIELDS,
    MilvusVectorDBStorage,
)
from lightrag.utils import EmbeddingFunc, Tokenizer

pytestmark = pytest.mark.offline

BIZ_DOC_ID_MAX_LENGTH = 1024


class _SimpleTokenizerImpl:
    def encode(self, content: str) -> list[int]:
        return [ord(ch) for ch in content]

    def decode(self, tokens: list[int]) -> str:
        return "".join(chr(t) for t in tokens)


async def _dummy_embedding(texts: list[str]) -> np.ndarray:
    return np.ones((len(texts), 8), dtype=float)


async def _dummy_llm(*args, **kwargs) -> str:
    return "ok"


def _make_chunks_storage() -> MilvusVectorDBStorage:
    mock_embedding_func = MagicMock()
    mock_embedding_func.embedding_dim = 128
    return MilvusVectorDBStorage(
        namespace="chunks",
        workspace="test_workspace",
        global_config={
            "embedding_batch_num": 100,
            "vector_db_storage_cls_kwargs": {
                "cosine_better_than_threshold": 0.3,
            },
        },
        embedding_func=mock_embedding_func,
        meta_fields=set(),
    )


def test_chunks_schema_contains_biz_doc_id_column():
    storage = _make_chunks_storage()

    fields_by_name = {
        field.name: field for field in storage._create_schema_for_namespace().fields
    }

    assert "biz_doc_id" in fields_by_name, (
        "biz_doc_id missing from the chunks Milvus schema — an upstream merge "
        "may have dropped the fork-custom FieldSchema block"
    )
    field = fields_by_name["biz_doc_id"]
    assert field.dtype == DataType.VARCHAR
    assert int(field.params["max_length"]) == BIZ_DOC_ID_MAX_LENGTH


def test_chunks_varchar_limits_include_biz_doc_id():
    storage = _make_chunks_storage()

    limits = storage._get_varchar_field_limits_for_namespace()

    assert limits.get("biz_doc_id") == BIZ_DOC_ID_MAX_LENGTH, (
        "biz_doc_id missing from the chunks varchar-limit manifest — the "
        "upsert sanitizer would no longer size-check the field"
    )


def test_chunks_migrated_metadata_limits_include_biz_doc_id():
    storage = _make_chunks_storage()

    limits = storage._get_migrated_metadata_field_limits()

    assert limits.get("biz_doc_id") == BIZ_DOC_ID_MAX_LENGTH, (
        "biz_doc_id missing from the migrated-metadata manifest — the "
        "automatic schema migration would no longer reconcile pre-existing "
        "chunks collections on startup"
    )


def test_chunks_required_fields_include_biz_doc_id():
    storage = _make_chunks_storage()

    required = storage._get_required_fields_for_namespace()

    assert required.get("biz_doc_id") == {"type": "VarChar"}


def test_biz_doc_id_is_an_identity_field_not_separator_joined():
    assert "biz_doc_id" in MILVUS_IDENTITY_VARCHAR_FIELDS, (
        "biz_doc_id missing from MILVUS_IDENTITY_VARCHAR_FIELDS — oversize "
        "values would no longer be rejected on the live path / truncated "
        "during migration"
    )
    # It is a single URL, never a GRAPH_FIELD_SEP-joined list.
    assert "biz_doc_id" not in MILVUS_SEPARATOR_JOINED_FIELDS


def test_lightrag_chunks_vdb_meta_fields_allow_biz_doc_id(tmp_path):
    rag = LightRAG(
        working_dir=str(tmp_path / "wd"),
        workspace=f"bizsent-{uuid4().hex[:8]}",
        llm_model_func=_dummy_llm,
        embedding_func=EmbeddingFunc(
            embedding_dim=8, max_token_size=8192, func=_dummy_embedding
        ),
        tokenizer=Tokenizer("mock-tokenizer", _SimpleTokenizerImpl()),
    )

    assert "biz_doc_id" in rag.chunks_vdb.meta_fields, (
        "biz_doc_id missing from chunks_vdb meta_fields — the Milvus writer "
        "would silently drop the field the ingestion dict carries"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
