## ADDED Requirements

### Requirement: Custom-chunk ingestion extracts biz_doc_id from onyx JSON payloads

`ainsert_custom_chunks` SHALL extract the top-level string `"id"` from each custom-chunk JSON payload and persist it as `biz_doc_id` on the chunk record. Any input that is not a JSON object with a string `"id"` (plain text, invalid JSON, non-object JSON, missing or non-string `"id"`) SHALL store an empty string. Extraction MUST NOT raise on any input.

#### Scenario: Onyx-style JSON chunk

- **WHEN** a custom chunk arrives whose text is `{"id": "https://host/browse/SCRUM-1", ...}`
- **THEN** the persisted chunk record carries `biz_doc_id = "https://host/browse/SCRUM-1"` in both the Milvus chunks collection and the Redis `text_chunks` KV record

#### Scenario: Non-matching payload

- **WHEN** a custom chunk arrives as plain text, invalid JSON, or JSON without a string `"id"`
- **THEN** the persisted chunk record carries `biz_doc_id = ""` and ingestion proceeds without error

### Requirement: Milvus chunks collection stores biz_doc_id as an indexed nullable column

The Milvus `chunks` collection schema SHALL include a nullable VARCHAR column `biz_doc_id` (max length 1024) with an INVERTED scalar index. The field SHALL be registered in every schema manifest: varchar limits, migrated-metadata limits (auto-migration trigger), required-fields report, and the identity over-length guard set.

#### Scenario: New collection creation

- **WHEN** the chunks collection is created by the storage backend
- **THEN** the schema contains `biz_doc_id` VARCHAR(1024) and an INVERTED index is created on it in both index-creation branches (`IndexParams` and fallback)

#### Scenario: Pre-existing collection without the column

- **WHEN** the backend starts against a chunks collection missing `biz_doc_id`
- **THEN** the automatic schema-migration check detects the missing field and migrates the collection so the column exists afterwards

#### Scenario: Oversize value

- **WHEN** an extracted `"id"` exceeds the 1024-byte limit
- **THEN** the live upsert path rejects the value (identity-field guard) while the migration path truncates it with a warning

### Requirement: biz_doc_id survives KV passthrough and offline VDB rebuild

The chunk record SHALL be written to the Redis `text_chunks` KV store unchanged (whole-dict passthrough, no KV-layer code changes), and `rebuild_vdb.py` SHALL keep `biz_doc_id` present on rebuilt payloads via an explicit empty-string default, mirroring the existing `full_doc_id` / `file_path` handling.

#### Scenario: KV record carries the field

- **WHEN** a custom chunk with an extracted `biz_doc_id` value is inserted
- **THEN** the Redis `text_chunks` record for that chunk contains the same `biz_doc_id` key

#### Scenario: Offline rebuild keeps the field present

- **WHEN** `rebuild_chunks_vdb` rebuilds the chunks vector storage from the KV store
- **THEN** each rebuilt payload carries `biz_doc_id` — the stored value, or `""` when the source KV record lacked the key
