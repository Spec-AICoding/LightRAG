# Design: Chunk `biz_doc_id` Persistence

## Architecture (data flow)

```
onyx push (s3_routes.py, unchanged)
  chunk_text = '{"id": "https://.../SCRUM-1", ...}'
        │
        ▼
LightRAG.ainsert_custom_chunks                      lightrag/lightrag.py
        │
        │  _build_inserting_chunks()  ← SINGLE chunk-dict construction point
        │    { "content", "full_doc_id", "tokens", "chunk_order_index",
        │      "file_path",
        │      "biz_doc_id": _extract_biz_doc_id(content) }   ← fork-custom
        ▼
   inserting_chunks (one shared dict, two writers)
        │
        ├──▶ chunks_vdb.upsert()   Milvus: meta_fields whitelist gates the copy
        │        (field silently dropped if not in meta_fields!)
        │
        └──▶ text_chunks.upsert()  Redis KV: whole dict stored → passthrough
                                   (zero KV-layer changes)

offline rebuild (rebuild_vdb.py, unchanged flow)
   text_chunks KV records ──▶ chunks_vdb.upsert()
        (payload passed through; setdefault keeps the field present)
```

## Decisions

### 1. Field name: `biz_doc_id` (not `biz_id`)

Graph entities carry a **multi-valued** `biz_id` array (one entity can be contributed by many documents). The chunk row is a **single-value** ownership reference. `biz_doc_id` keeps the two distinct in logs, Cypher and Milvus filters.

### 2. Extraction point: `_build_inserting_chunks`, via one pure helper

`_build_inserting_chunks` (inside `ainsert_custom_chunks`) is the only place the custom-chunk dict is built, and it feeds both Milvus and Redis from the same dict. Injection = 1 dict line.

New module-level pure function in `lightrag/lightrag.py` (single exit semantics, never raises):

```python
# fork-custom (biz_doc_id): extract the onyx document URL from a custom-chunk
# JSON payload; anything else (plain text, non-dict JSON, missing/non-string
# "id") yields "" so non-onyx content is stored as empty rather than dropped.
def _extract_biz_doc_id(text: str) -> str:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(payload, dict):
        value = payload.get("id")
        if isinstance(value, str):
            return value
    return ""
```

No URL-shape validation (keeps parity with the existing `biz_id` injection in `s3_routes.py`, which stores the raw `"id"` value). `json.loads` cost on the custom-chunk path is accepted — it is a low-frequency batch entry, and `_build_inserting_chunks` already tokenizes every chunk.

### 3. Column length: 1024

- Reference points: `full_doc_id` = 64 (hash id), `file_path` = 32768 (separator-joined path list), `entity_name` = 512.
- `biz_doc_id` is a single URL (tens to low hundreds of chars; Jira example is 46). 1024 is ample headroom, far below `file_path`, so it cannot be mistaken for a list field.
- Inline literal in `_get_varchar_field_limits_for_namespace` (upstream style keeps these numeric literals inline).

### 4. Over-length guard: `MILVUS_IDENTITY_VARCHAR_FIELDS`

`biz_doc_id` is a single-value identity-class field, so add it to the identity set, gaining upstream behaviour for free:
- live upsert path: oversize value is **rejected** (caller fixes input);
- migration path: oversize legacy value is **truncate-and-warn** instead of aborting the collection migration.

Not added to `MILVUS_SEPARATOR_JOINED_FIELDS` (that set is for GRAPH_FIELD_SEP-joined lists).

### 5. Index: INVERTED on `biz_doc_id`

Feature 2 filters with `biz_doc_id in [...]`, the same membership shape `full_doc_id` uses. Add the scalar index in both creation branches (`_create_indexes_after_collection`: IndexParams branch + fallback branch) mirroring the `full_doc_id` entries. Schema-migration rebuilds go through collection re-creation, so migrated collections get the index too.

### 6. Pre-existing collections: drop (primary) + auto-migration (free fallback)

Constraint from the user: **no historical-data handling**. Two paths, both acceptable:

| Path | Mechanism | Data |
|---|---|---|
| Primary (per the constraint) | Drop the chunks collection before first boot with the new code; startup recreates it with the new schema + index | discarded (acceptable) |
| Free fallback | `biz_doc_id` registered in `_get_migrated_metadata_field_limits()` → `_check_metadata_schema_migration_needed()` detects the missing column and `_migrate_collection_schema()` (existing temp-collection copy) reconciles automatically | preserved (rows get NULL `biz_doc_id`) |

Why register the fallback (1 line) even though history is out of scope: it reuses the upstream mechanism verbatim (no new code path), and it removes the "forgot to drop" failure mode — with `enable_dynamic_field=True` an unregistered write would otherwise land in the Milvus dynamic field, invisible to a typed `in` filter later. Without registration the failure is silent; with it, startup self-heals.

`_get_required_fields_for_namespace` also gets the key so the "missing optional fields" startup report stays truthful.

### 7. Redis and rebuild paths

- Redis `text_chunks` stores the whole dict → `biz_doc_id` arrives with **zero changes** to `RedisKVStorage` or any KV backend.
- `rebuild_vdb.py::rebuild_chunks_vdb` passes KV records through; add `payload.setdefault("biz_doc_id", "")` next to the existing `full_doc_id` / `file_path` setdefaults (1 line) so records written before this change (or by third parties) still upsert with an explicit empty value instead of relying on NULL.

## Merge-clean contract

The user constraint: keep future merges with upstream `main` as cheap as possible. This is the primary engineering constraint of this change.

### Full edit manifest (all additive, no line modified or removed)

| File | Anchor | Edit shape |
|---|---|---|
| `lightrag/lightrag.py` | import block (after `import contextvars`) | +1 import line (`import json`) |
| `lightrag/lightrag.py` | module level above the `LightRAG` class, next to `_run_sync` | +1 marked pure helper (~14 lines) |
| `lightrag/lightrag.py` | `chunks_vdb` construction: `meta_fields={"full_doc_id", "content", "file_path"}` | add 1 set element |
| `lightrag/lightrag.py` | `_build_inserting_chunks` dict | +1 dict line (calls helper) |
| `milvus_impl.py` | `MILVUS_IDENTITY_VARCHAR_FIELDS` | +1 frozenset element |
| `milvus_impl.py` | `_create_schema_for_namespace` chunks `specific_fields` | +1 FieldSchema block (5 lines) |
| `milvus_impl.py` | `_get_varchar_field_limits_for_namespace` chunks | +1 dict entry |
| `milvus_impl.py` | `_get_migrated_metadata_field_limits` chunks | +1 dict entry |
| `milvus_impl.py` | `_get_required_fields_for_namespace` chunks | +1 dict entry |
| `milvus_impl.py` | `_create_indexes_after_collection` chunks (IndexParams branch) | +1 add_index call |
| `milvus_impl.py` | `_create_indexes_after_collection` chunks (fallback branch) | +1 fallback call |
| `rebuild_vdb.py` | `rebuild_chunks_vdb` payload prep | +1 setdefault |

### Rules applied

1. **Additive only** — no existing line deleted/modified, no signature change, no reordering. Upstream diffs touch our lines only within the same hunk; resolution is "keep both".
2. **`fork-custom (biz_doc_id)` marker comments** on the helper and the two non-obvious manifest entries (schema field + migration trigger), so a future conflict resolution and a future cleanup both find them instantly.
3. **No new abstractions** — no config flags, no environment variables, no base-class hooks. Every upstream subsystem (config, factory, base classes) stays untouched, minimising interaction surface.
4. **Known overlap zone** — `lightrag.py` `ainsert_custom_chunks` is upstream's highest-churn area (141 commits / 3 months) and has already conflicted once at the `file_path` patch. The helper lives at module scope (above the `LightRAG` class, far from the churn zone), the dict line is a single line inside a small closure: the two fork edits (file_path param + biz_doc_id line) stay adjacent and are resolvable as one block.
5. **Sentinel tests** — an automated pairing check (below) that fails loudly if a future upstream merge silently drops either half (meta_fields entry or schema manifest entry), because the failure mode without it is a silent field drop.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Silent field drop: `biz_doc_id` absent from `meta_fields` while present in the dict → Milvus ignores it | Sentinel test asserting the pairing on both sides; marker comments |
| Manifest drift: added to schema but not to migration/limits/required manifests | Each manifest anchored to a dedicated task checklist item + sentinel assertions |
| Upstream merge conflict in `lightrag.py` closure area | Edits are single marked lines, adjacent to the existing file_path patch; resolvable as one block |
| Oversize `"id"` value (> 1024) | Identity-field guard: live path rejects loudly, migration truncates with warning |
| Non-onyx callers of `ainsert_custom_chunks` | Helper returns `""` for any non-matching payload; behaviour of those chunks unchanged (NULL/empty, never filtered positively in Feature 2 unless whitelisted) |

## Test plan

- **Unit — helper**: plain text → `""`; non-JSON → `""`; JSON array/scalar → `""`; dict without `id` → `""`; `id` non-string → `""`; dict with string `id` → verbatim; whitespace / unicode URL preserved. (New `tests/test_extract_biz_doc_id.py`, following the existing root-level micro-test style.)
- **Sentinel — schema pairing** (new `tests/kg/milvus_impl/` test, following existing Milvus test patterns): `biz_doc_id` present in `_create_schema_for_namespace` chunks output, in `_get_varchar_field_limits_for_namespace`, in `_get_migrated_metadata_field_limits`, in `_get_required_fields_for_namespace`, and in `MILVUS_IDENTITY_VARCHAR_FIELDS`; plus the LightRAG-side `chunks_vdb` `meta_fields` contains it.
- **Existing suites**: run `tests/kg/milvus_impl/` and `tests/pipeline/` subsets to confirm zero regressions (schema snapshot tests, if any, will flag expected deltas).

## Deployment note

With the "no historical data" constraint: stop writers → drop the `chunks` collection (Milvus) → start the new build (collection recreated with `biz_doc_id` + index) → new custom-chunk inserts carry the field; previously inserted chunks do not (acceptable). If the collection is *not* dropped, startup auto-migration reconciles it (see Decision 6).
