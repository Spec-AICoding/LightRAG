# Tasks: add-query-acl-filter

Repo paths below are relative to each repository root.

## LightRAG

### 1. Storage — Milvus schema (chunks + entities)

- [x] 1.1 Add three ACL columns to the chunks collection schema in `lightrag/kg/milvus_impl.py`: `acl_is_public` (BOOL, nullable), `acl_external_user_emails` / `acl_external_user_group_ids` (ARRAY\<VARCHAR\>, element max_length 512, max_capacity 64); each addition `fork-custom (query-acl)` marked
- [x] 1.2 Add the same three ACL columns to the entities collection schema (union snapshot semantics); register all fields in every manifest site (varchar limits, required-fields report); create INVERTED indexes in both branches (`IndexParams` and fallback) for both collections
- [x] 1.3 `lightrag/tools/rebuild_vdb.py`: passthrough with explicit defaults (`false` / empty lists) for both chunks and entities, mirroring `full_doc_id` / `file_path` handling
- [ ] 1.4 Recreate both collections via rebuild and verify the schema (no ARRAY auto-migration; no historical-data adaptation)

### 2. Ingestion snapshot writing

- [x] 2.1 Add ACL extraction helper in `lightrag/lightrag.py` next to `_extract_biz_doc_id`: parse top-level `external_access` of the batch-JSON document object; malformed/missing → null fields, never raise; `fork-custom (query-acl)` marked
- [x] 2.2 Wire the helper into the `ainsert_custom_chunks` payload path driven by `lightrag/api/routers/s3_routes.py` so every chunk row carries the flattened ACL
- [x] 2.3 New entity-union module: `union_acl(existing_snapshot, doc_acl)` helper (OR for is_public, set-union for emails/groups)
- [x] 2.4 Wire the union helper into the `merge_nodes_and_edges` vdb_data construction in `lightrag/operate.py` (ONE call, `fork-custom (query-acl)` marked — re-anchor on upstream refactor)
- [x] 2.5 Wire the union helper into the `ainsert_custom_kg` entity payload in `lightrag/lightrag.py` (ONE call, same discipline)

### 3. Identity transport

- [x] 3.1 New module (e.g. `lightrag/api/routers/acl_identity.py` or `lightrag/acl_identity.py`): identity ContextVar holding `(emails, groups)` with set/get accessors
- [x] 3.2 `QueryRequest`: optional multi-valued `user_emails` / `user_group_ids`; exclude both in `to_query_params`; the three handlers (`/query/data`, `/query/stream`, `/query`) set the ContextVar (and reset it after)

### 4. Filtering

- [x] 4.1 New expr-builder module: build the chunk expr from the ContextVar identity (`acl_is_public == true OR ARRAY_CONTAINS_ANY(...)`); no identity → `acl_is_public == true` only; the same builder serves the entities recall (identical shape against the union snapshot)
- [x] 4.2 `milvus_impl.query`: append the expr when identity is present for both the chunks and entities namespaces (additive `fork-custom (query-acl)` block, signature unchanged)
- [x] 4.3 New filter-implementation module: ② collapse query (`id in [...] AND <acl expr>` → visible subset, including the recalled relations' `source_id` chunks) and ③ authority judgment (entity union-ACL on the in-memory node data; relation visibility iff a lineage chunk is visible)
- [x] 4.4 `operate.py`: invoke the ContextVar-registered filter callback after `_merge_all_chunks`, before truncation (~10 additive lines, `fork-custom (query-acl)` marked; unset by default)
- [x] 4.5 Handler wrapper: expand `top_k` by a fixed factor before calling `aquery*` when identity is present (recall-headroom compensation; relation recall is not seat-filtered)

### 5. Internal ACL-update endpoints

- [x] 5.1 Fork router endpoint (e.g. `POST /acl/chunks` in a fork-owned router, combined_auth protected): `{biz_doc_id, is_public, emails, groups}` → resolve chunk ids via `biz_doc_id == $doc` → upsert the three ACL fields only
- [x] 5.2 Fork router endpoint for the entity recompute chain (e.g. `POST /acl/entities`): graph reverse-lookup of entities whose `biz_id` contains the document → fetch contributing documents' ACLs from the chunks collection → OR-merge → batch-upsert the entities rows' three ACL fields only
- [x] 5.3 Bounded batches + upsert validation (ARRAY capacity guard mirrors live upsert path)

### 6. Tests (LightRAG)

- [x] 6.1 Expr-builder unit tests: with identity, without identity, empty lists (chunks and entities namespaces)
- [x] 6.2 Milvus recall prefilter: private chunk invisible to non-member; null ACL invisible; public row always visible
- [x] 6.3 Entity seat filter: invisible entity kept out of top_k seats; stale snapshot lets entity through and the hook still drops it
- [x] 6.4 ② collapse query: fallback chunk hidden / kept; chunk id absent from Milvus → invisible
- [x] 6.5 ③ authority judgment: hidden entity dropped (graph union); relation with all-invisible lineage dropped; union visibility
- [x] 6.6 Endpoint tests: identity fields accepted on all three endpoints; excluded from `QueryParam`; ContextVar set/reset
- [x] 6.7 Ingestion snapshot writing: chunk flattening with / without / malformed `external_access`; entity union accumulation across batches (A then B → union of both)
- [x] 6.8 Internal endpoints: chunk update by `biz_doc_id`; entity recompute chain (incl. failure → seats degrade, no leak); upsert validation; auth
- [x] 6.9 rebuild passthrough: rebuilt chunks/entities payloads carry the ACL fields with defaults

## Onyx

### 7. Query side — identity forwarding

- [x] 7.1 `graph_knowledge_search.py`: payload gains `user_emails` / `user_group_ids` sourced from the retrieval request's user context; omit when unavailable
- [x] 7.2 Thread the requester identity through the search pipeline (search runner / chunk-index request context) to the graph API call site

### 8. Push side — GRAPH_ACL_PUSH extension

- [x] 8.1 After the Neo4j union update for a document, call the LightRAG internal endpoints in order: `/acl/chunks` (row snapshot), then `/acl/entities` (union recompute)
- [x] 8.2 Retry/error handling: a failed update is recorded and heals on the next push replay (matches the accepted residues; entity-recompute failure degrades seats only)

### 9. Tests (Onyx)

- [ ] 9.1 search_graph payload includes identity when the retrieval context has one; omitted otherwise
- [ ] 9.2 GRAPH_ACL_PUSH calls both endpoints in order after the graph update (mocked endpoints); recompute failure does not block the chunk-row update

<!-- 9.1/9.2 written (`backend/tests/unit/onyx/context/search/graph_search/test_graph_acl_identity.py`, `backend/tests/unit/onyx/background/celery/tasks/test_graph_acl_push_lightrag.py`), py_compile + static review done; NOT EXECUTED — the local Onyx venv is empty and Docker is down, needs the user's Onyx test environment to run -->

## Verification

- [ ] 10.1 URL normalization: confirm perm-sync `doc_id`, row `biz_doc_id`, entity `biz_id`, and the recompute chain's document lookup share one URL space against real data before wiring the push update
- [ ] 10.2 End-to-end (local stack): query with identity (mixed visibility), without identity (public-only), across mix/local/global/naive modes; `/query/stream` and `/query` show no invisible content in the LLM context
- [x] 10.3 Register the ACL regression subset (tasks 6.x) in the upstream-merge checklist as a mandatory post-merge run; include the new-path hook-coverage check
- [x] 10.4 Confirm magicbox eval panel behavior: identity omitted → public-only results (documented behavior change)

<!-- 10.1/10.2 need the real environment (live stack + real data); the same applies to 1.4, 9.1/9.2 (Onyx test env) -->
