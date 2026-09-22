# Persist Chunk Ownership: `biz_doc_id` on the Milvus Chunks Collection

## Summary

Stamp every chunk row written through `ainsert_custom_chunks` with a `biz_doc_id` — the onyx document URL extracted from the top-level `"id"` field of the custom-chunk JSON payload. The value lands as:

1. a new nullable VARCHAR column `biz_doc_id` on the Milvus `chunks` collection (with an INVERTED scalar index),
2. a passthrough key on the Redis `text_chunks` KV record (rides the shared chunk dict — zero KV-layer changes),
3. a carried-through field on the offline VDB rebuild path (`rebuild_vdb.py`).

Non-JSON chunks and JSON payloads without a string `"id"` store an empty string (the ordinary file-ingestion pipeline is untouched and stores nothing).

## Motivation

Onyx pushes custom chunks whose `chunk_text` is a JSON object carrying the source document URL in its top-level `"id"` field (e.g. `https://wpf0310.atlassian.net/browse/SCRUM-1`). Graph entities already carry that URL as `biz_id` via the from-s3 injection in `s3_routes.py`, but **chunk rows have no ownership metadata**. Without it, a permission-scoped retrieval has no storage-level hook to exclude chunks belonging to documents a user cannot see.

`biz_doc_id` is the single-value ownership reference (distinct from the graph entity's multi-valued `biz_id` array) that unlocks engine-level filtering in Feature 2: Milvus `expr: biz_doc_id in [visible document set]` at vector-recall time.

## Goals

- New nullable `biz_doc_id` VARCHAR column on the Milvus `chunks` collection with an explicit varchar limit and an INVERTED scalar index (both index-creation branches: `IndexParams` and fallback).
- Extraction helper parse-safely reads the top-level string `"id"` from custom-chunk JSON; every non-matching input yields `""` (never raises).
- Registered in **every** Milvus field manifest — schema columns, varchar limits, auto-migration trigger list (`_get_migrated_metadata_field_limits`), required-fields report, and the over-length identity-field guard — so the upstream automatic schema-migration mechanism reconciles pre-existing collections on startup.
- `rebuild_vdb.py` passthrough keeps the field present with the same `setdefault(..., "")` pattern as `full_doc_id` / `file_path`.
- **Merge-friendly by construction**: every edit is an additive line or block with a `fork-custom (biz_doc_id)` marker comment; no signature changes, no control-flow edits, no new abstractions. See design.md "Merge-clean contract".

## Non-goals

- **No backfill of pre-existing data and no migration scripts** — current environments accept dropping / rebuilding the chunks collection; historical chunks (ingested before this change) will not carry `biz_doc_id`.
- No query-side consumption — wiring `biz_doc_id` into retrieval filtering is Feature 2, a separate change.
- No change to `ainsert_custom_kg` chunks, the ordinary document pipeline, the Redis KV layer, or `s3_routes.py`.
- No REST/UI surface: the field is write-side storage only in this change.

## Impact

- `lightrag/lightrag.py` — 1 import line, 1 meta_fields line, 1 dict line, 1 new module-level pure helper.
- `lightrag/kg/milvus_impl.py` — 7 additive edits across 5 schema-manifest sites (≈ 25 lines).
- `lightrag/tools/rebuild_vdb.py` — 1 `setdefault` line.
- New tests only; no existing tests change.
