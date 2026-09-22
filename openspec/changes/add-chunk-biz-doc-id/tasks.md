# Tasks: Chunk `biz_doc_id` Persistence

Execution order: ① helper → ② injection → ③ Milvus manifests → ④ rebuild passthrough → ⑤ tests → ⑥ verification.

**Merge-clean rules for every step** (design.md "Merge-clean contract"): edits are **additive only** — never delete, reformat, reorder or rewrite surrounding upstream lines; mark fork edits with `fork-custom (biz_doc_id)` comments; do not change any function signature. Anchors below are function/constant names — line numbers drift with upstream merges.

---

## 1. Extraction helper + injection (`lightrag/lightrag.py`)

- [x] Add `import json` to the module import block (after `import contextvars`) — required by the helper
- [x] Add module-level pure helper `_extract_biz_doc_id(text: str) -> str` at module level above the `LightRAG` class (next to `_run_sync`), with `fork-custom (biz_doc_id)` marker comment (exact body in design.md Decision 2): parses top-level string `"id"` from JSON; any non-matching input (plain text, non-JSON, non-dict JSON, missing/non-string `id`) returns `""`; never raises
- [x] `_build_inserting_chunks` closure dict (inside `ainsert_custom_chunks`): add `"biz_doc_id": _extract_biz_doc_id(content),` — the single line that feeds both Milvus and Redis
- [x] `chunks_vdb` construction (`__init__`): extend `meta_fields` from `{"full_doc_id", "content", "file_path"}` to include `"biz_doc_id"` — without this the Milvus writer silently drops the field

## 2. Milvus schema manifests (`lightrag/kg/milvus_impl.py`)

All 7 edits are one-liners or one small block; nothing else in the file changes.

- [x] `MILVUS_IDENTITY_VARCHAR_FIELDS`: add `"biz_doc_id"` (live upsert rejects oversize; migration truncates-and-warns)
- [x] `_create_schema_for_namespace`, chunks `specific_fields`: add nullable `biz_doc_id` FieldSchema block (`max_length=varchar_limits["biz_doc_id"]`), marked with `fork-custom (biz_doc_id)`
- [x] `_get_varchar_field_limits_for_namespace`, chunks branch: add `"biz_doc_id": 1024` to the returned dict
- [x] `_get_migrated_metadata_field_limits`, chunks branch: add `"biz_doc_id": 1024` — this registers the auto-migration trigger for pre-existing collections, marked with `fork-custom (biz_doc_id)`
- [x] `_get_required_fields_for_namespace`, chunks branch: add `"biz_doc_id": {"type": "VarChar"}` (keeps the startup "missing optional fields" report truthful)
- [x] `_create_indexes_after_collection`, chunks `IndexParams` branch: add `biz_doc_id` INVERTED index call mirroring the `full_doc_id` block (including its except → `_create_scalar_index_fallback` degrade path)
- [x] `_create_indexes_after_collection`, chunks fallback branch (the `if scalar_index_params is not None` else-side): add `self._create_scalar_index_fallback("biz_doc_id", "INVERTED")` beside the `full_doc_id` fallback

## 3. Rebuild passthrough (`lightrag/tools/rebuild_vdb.py`)

- [x] `rebuild_chunks_vdb`, payload preparation: add `payload.setdefault("biz_doc_id", "")` beside the existing `full_doc_id` / `file_path` setdefaults

## 4. Tests

- [x] New `tests/test_extract_biz_doc_id.py` (root-level micro-test style): plain text → `""`; invalid JSON → `""`; JSON array / scalar → `""`; dict without `id` → `""`; non-string `id` → `""`; string `id` → verbatim (incl. URL with path/query, non-ASCII)
- [x] New `tests/kg/milvus_impl/test_milvus_chunk_biz_doc_id.py` (sentinel, following existing test patterns in that directory): `biz_doc_id` present in all five Milvus manifests (schema fields / varchar limits / migrated-metadata limits / required fields / identity set) **and** in the LightRAG `chunks_vdb` `meta_fields` — fails loudly if a future merge drops either half
- [x] Run `./scripts/test.sh tests/test_extract_biz_doc_id.py tests/kg/milvus_impl/` and the `tests/pipeline/` subset — report pass counts, zero regressions
  - Results (lightrag conda env, `PYTHON=...`): micro-test 11 passed; sentinel 6 passed; full `tests/kg/milvus_impl/` 208 passed / 1 failed; `tests/pipeline/` 873 passed / 2 failed. All 3 failures reproduce identically with this change stashed (pre-existing environment leak: the repo `.env` injects `MILVUS_USER`/`MILVUS_PASSWORD` into a client-arg assertion, plus two warning-format assertions) — zero regressions from this change.

## 5. Verification (end-to-end)

Precondition per the no-history constraint: the existing chunks collection is **dropped, not migrated** (data re-ingestible / expendable).

- [ ] Stop the LightRAG server (no concurrent writers)
- [ ] Drop the chunks collection in Milvus
- [ ] Start the new build → collection recreated; verify schema via Milvus describe: `biz_doc_id` VARCHAR(1024) present and INVERTED-indexed
- [ ] Insert one onyx-style custom chunk (JSON payload with top-level `"id"` URL) through the normal path (`ainsert_custom_chunks` / s3 route) → Milvus row has `biz_doc_id` = the URL; Redis `text_chunks` record carries the same key
- [ ] Insert one plain-text custom chunk → `biz_doc_id` = `""` (no error)
- [ ] (Free fallback check, optional) With a pre-existing collection lacking `biz_doc_id`, startup logs `Starting automatic migration for collection ...` and the column exists afterwards
