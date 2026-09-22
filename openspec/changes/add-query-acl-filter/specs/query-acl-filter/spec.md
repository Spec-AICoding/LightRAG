## ADDED Requirements

### Requirement: Query endpoints accept the requesting user's identity

`QueryRequest` on `/query/data`, `/query/stream`, and `/query` SHALL accept optional multi-valued `user_emails` and `user_group_ids` fields. The fields SHALL be excluded from `to_query_params` mapping (never forwarded into `QueryParam`); the handlers SHALL place the identity into the request-scoped identity ContextVar before invoking the query.

#### Scenario: Identity provided

- **WHEN** a request carries `user_emails=["a@x.com"]` and `user_group_ids=["confluence_team-a"]`
- **THEN** the retrieval filters chunks, entities, and relationships against that identity across all three endpoints

#### Scenario: Identity omitted

- **WHEN** a request carries neither `user_emails` nor `user_group_ids`
- **THEN** the retrieval returns public-only data (`acl_is_public == true` rows; entities with `is_public == true`); private rows and entities are invisible — fail-closed, not an error

#### Scenario: Empty identity lists

- **WHEN** a request carries empty lists for both fields
- **THEN** behavior is identical to omitting the fields (public-only)

### Requirement: Milvus chunks collection stores the ACL snapshot as indexed nullable columns

The Milvus `chunks` collection schema SHALL include `acl_is_public` (BOOL, nullable), `acl_external_user_emails` and `acl_external_user_group_ids` (ARRAY\<VARCHAR\>, nullable, element max_length 512, max_capacity 64). All three SHALL carry an INVERTED index in both index-creation branches (`IndexParams` and fallback), and SHALL be registered in every schema manifest site (varchar limits, required-fields report).

#### Scenario: New collection creation

- **WHEN** the chunks collection is created by the storage backend
- **THEN** the schema contains the three ACL columns and INVERTED indexes are created on them

#### Scenario: Pre-existing collection

- **WHEN** the backend starts against a chunks collection missing the ACL columns
- **THEN** no ARRAY auto-migration is attempted; the collection is recreated via `rebuild_vdb.py` (explicit decision — no historical data)

### Requirement: Milvus entities collection stores the union ACL snapshot

The Milvus `entities` collection schema SHALL include the same three nullable ACL columns as chunks. Their values SHALL be the **union snapshot** of every contributing document's ACL: `acl_is_public` is the OR of the contributing documents' `is_public`; `acl_external_user_emails` / `acl_external_user_group_ids` are the union of the contributing documents' lists. All three SHALL carry INVERTED indexes and manifest registration as on chunks.

#### Scenario: Entities schema includes the snapshot columns

- **WHEN** the entities collection is created by the storage backend
- **THEN** the schema contains the three ACL columns with INVERTED indexes

#### Scenario: Snapshot is the union of contributions

- **WHEN** an entity accumulated contributions from document A (`is_public=true`) and document B (`emails=["b@x.com"]`)
- **THEN** the entity row's `acl_is_public` is true and `acl_external_user_emails` contains `b@x.com`

### Requirement: Ingestion writes the ACL snapshots

The custom-chunk ingestion path (`ainsert_custom_chunks`, driven by `s3_routes`) SHALL extract the batch-JSON document object's top-level `external_access` (`is_public`, `external_user_emails`, `external_user_group_ids`) and flatten it onto every chunk row of that document. The same document ACL SHALL feed the entity union snapshot written during the merge step (union of the new batch's document ACL with the entity row's existing snapshot). Inputs without a usable `external_access` SHALL leave the ACL fields null and MUST NOT raise.

#### Scenario: Batch object with external_access

- **WHEN** a batch-JSON document object carries `external_access = {"is_public": false, "external_user_emails": ["a@x.com"], "external_user_group_ids": ["confluence_team-a"]}`
- **THEN** every chunk row produced from it carries the same values in `acl_is_public` / `acl_external_user_emails` / `acl_external_user_group_ids`

#### Scenario: Entity snapshot accumulates across batches

- **WHEN** an entity already contributed by document A is merged again from document B with a different ACL
- **THEN** the entity row's snapshot is the union of A's and B's ACLs (not B's alone)

#### Scenario: Payload without external_access

- **WHEN** the document object lacks `external_access` or it is malformed
- **THEN** the rows' ACL fields are null (invisible under filtering) and ingestion proceeds without error

### Requirement: Vector chunk recall filters in the engine

When the identity ContextVar is set, the Milvus chunks vector recall SHALL append a scalar expr equivalent to `acl_is_public == true OR ARRAY_CONTAINS_ANY(acl_external_user_emails, $emails) OR ARRAY_CONTAINS_ANY(acl_external_user_group_ids, $groups)`; without identity it SHALL use `acl_is_public == true` only. The expr SHALL be built by a dedicated builder module and attached inside `milvus_impl.query` without changing its signature.

#### Scenario: Private chunk invisible to non-member

- **WHEN** a chunk row has `acl_is_public=false` and neither its emails nor groups match the requester's identity
- **THEN** the vector recall cannot return that chunk regardless of similarity

#### Scenario: Null ACL fields are invisible

- **WHEN** a chunk row's ACL fields are null or absent
- **THEN** the expr evaluates false and the row is invisible (fail-closed)

### Requirement: Entity recall seat-filters by the row union snapshot

When the identity ContextVar is set, the Milvus entities vector recall SHALL append the same expr shape against the row union snapshot. This filter is a **seat filter**: it may use a stale snapshot, and the authority judgment (below) SHALL re-verify every recalled entity regardless.

#### Scenario: Invisible entity kept out of top_k seats

- **WHEN** an entity's union snapshot matches neither the requester's identity nor `is_public`
- **THEN** the entity recall cannot return that entity regardless of similarity

#### Scenario: Stale snapshot does not leak

- **WHEN** the union snapshot is stale and lets an entity through the seat filter
- **THEN** the authority judgment still drops the entity when the graph union properties do not match the requester

### Requirement: source_id fallback chunks collapse back into Milvus for the decision

Chunks obtained through the entity/relation `source_id` fallback (KV `get_by_ids`) SHALL be re-judged by one Milvus query `id in [<chunk ids>] AND <acl expr>`; the returned ids are the visible subset. The id set SHALL include the recalled relations' `source_id` chunks so relation judgment shares the same query. KV records carry no ACL fields and none are added. Chunk ids absent from Milvus SHALL be treated as invisible.

#### Scenario: Fallback chunk hidden

- **WHEN** a source_id fallback retrieves a chunk whose row is not visible to the requester
- **THEN** the chunk is dropped before truncation and context building

### Requirement: Hook performs the authoritative entity and relation judgment

After retrieval and before rerank/truncation, a single filter hook (ContextVar-registered callback, unset by default) SHALL make the final visibility judgment:

- an entity SHALL be visible when its Neo4j union properties satisfy `is_public == true OR external_user_emails ∩ requester_emails ≠ ∅ OR external_user_group_ids ∩ requester_groups ≠ ∅` (in-memory on the fetched node data — the authority layer, overriding the seat filter);
- a relationship SHALL be visible when its `source_id` lineage contains at least one chunk visible in the collapse query (judgment anchored to the description's source documents).

#### Scenario: Hidden entity dropped

- **WHEN** a recalled entity's union ACL properties match neither the requester's emails nor groups and `is_public` is false
- **THEN** the entity is dropped from the response/context

#### Scenario: Relation from an invisible document is hidden

- **WHEN** a relation's endpoints are visible but every chunk in its `source_id` lineage is invisible to the requester
- **THEN** the relation is dropped (its description originates from documents the requester cannot see)

#### Scenario: Union visibility

- **WHEN** an entity accumulated contributions from a visible and an invisible document
- **THEN** the entity is visible (union semantics), including its merged description — accepted residue

### Requirement: GRAPH_ACL_PUSH updates the snapshots via the internal endpoints

A fork-owned LightRAG endpoint SHALL accept an ACL update `{biz_doc_id, is_public, emails, groups}`, resolve the affected chunk ids via `biz_doc_id == $doc`, and upsert the three ACL fields on those rows only. GRAPH_ACL_PUSH (Onyx) SHALL call this endpoint after its Neo4j union update for the same document. A second endpoint (or the same, split by concern) SHALL run the entity recompute chain for the affected document's entities: graph reverse-lookup of entities whose `biz_id` contains the document → gather each entity's contributing documents' ACLs → OR-merge → batch-upsert the entities rows' three ACL fields. Update order SHALL be Neo4j first, chunk rows second, entity rows last.

#### Scenario: Permission change propagates to all three targets

- **WHEN** a document's ACL changes and GRAPH_ACL_PUSH processes the perm-sync four-tuple
- **THEN** the Neo4j entity union updates first, then the document's chunk rows, then the affected entities' row snapshots carry the recomputed union

#### Scenario: Failed entity recompute degrades seats only

- **WHEN** the entity recompute chain fails
- **THEN** retrieval stays safe (the hook still judges by the graph union) and only recall-seat efficiency degrades until the next push replay

#### Scenario: Push replay heals staleness

- **WHEN** a previous push update failed and the next push for the same document succeeds
- **THEN** the rows converge to the latest ACL values (stale-permission window closes)

### Requirement: Offline rebuild preserves the ACL fields

`rebuild_vdb.py` SHALL keep the three ACL fields present on rebuilt payloads of both the chunks and entities collections via explicit defaults (`false` / empty lists), mirroring the existing `full_doc_id` / `file_path` passthrough pattern.

#### Scenario: Rebuild keeps the fields

- **WHEN** `rebuild_chunks_vdb` / `rebuild_entities_vdb` rebuilds a collection from the KV store
- **THEN** each rebuilt payload carries the three ACL fields — the stored values, or the explicit defaults when the source KV record lacked them

### Requirement: Onyx graph search forwards the requester's identity

Onyx's `search_graph` SHALL include `user_emails` and `user_group_ids` in the `/query/data` payload, sourced from the retrieval request's user context, when that context is available; otherwise it SHALL omit the fields (public-only, matching the fail-closed contract).

#### Scenario: Identity forwarded

- **WHEN** the Onyx search pipeline runs with a known requester
- **THEN** the graph API payload carries the requester's emails and group ids

#### Scenario: No identity available

- **WHEN** the retrieval context carries no user identity
- **THEN** the fields are omitted and the graph API returns public-only chunks
