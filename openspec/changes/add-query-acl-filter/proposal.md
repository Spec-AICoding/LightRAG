# Add User/Group ACL Filtering to the Query APIs

## Summary

Add permission-scoped retrieval to the three query endpoints (`/query/data`, `/query/stream`, `/query`): callers pass the requesting user's identity (`user_emails[]`, `user_group_ids[]`, both optional and multi-valued), and every retrieval surface filters against document-level ACL snapshots stored inline on the Milvus chunks rows, plus a union snapshot on the entities rows and the existing union ACL properties on graph entities.

Access decisions split into two layers. A **seat layer** filters at vector-recall time via Milvus scalar `expr` so `top_k` comes from the visible set: the chunks recall and the entities recall (union snapshot on the row). An **authority layer** — a single post-recall filter hook — is the final judgment: chunk lineage falls back to one collapse query on the chunks rows, entity visibility is re-verified against the graph's union properties (push updates the graph first), and relations are judged by anchoring their `source_id` lineage into the same collapse query. Callers that omit identity get public-only data (fail-closed); rows without ACL fields are invisible.

## Motivation

Onyx's graph search is the production retrieval channel — it takes `/query/data`'s chunks and feeds them into end-user search — yet today it returns unfiltered data. The graph's union ACL properties (injected since `add-connector-acl-graph-filtering`) only cover entity/relationship retrieval; the chunk path (Milvus vector recall + the source_id fallback through KV) never touches permission data at all. Chunk ownership (`biz_doc_id`, landed in `add-chunk-biz-doc-id`) established the chunk→document link; this change completes the chain by storing the document-level ACL snapshot on the same rows, so the permission judgment never leaves the storage engine on the chunk path.

The entities rows additionally gain a union ACL snapshot whose only job is recall-seat efficiency (keeping invisible entities out of `top_k`) — it is not the safety boundary. In multi-tenant deployments where a requester can see only a fraction of the corpus, post-recall filtering alone would systematically starve `top_k`; the recall-time filter fixes that without touching the judgment semantics.

The authoritative ACL source remains Onyx PostgreSQL (perm-sync four-tuple: `doc_id`, `external_user_emails`, `external_user_group_ids`, `is_public`). Data reaches the filter points at **write time** — flattened inline during ingestion and refreshed by GRAPH_ACL_PUSH on permission change — never via a read-time cross-service fetch.

## Goals

- Milvus `chunks` collection gains three nullable ACL columns: `acl_is_public` (BOOL), `acl_external_user_emails` and `acl_external_user_group_ids` (ARRAY\<VARCHAR\>), each with an INVERTED index; registered in every schema manifest site (mirroring the `biz_doc_id` pattern).
- Milvus `entities` collection gains the same three nullable ACL columns holding the **union snapshot** of all contributing documents' ACLs (is_public OR-merged, email/group lists unioned); INVERTED indexes and manifest registration as above.
- Ingestion flattens each batch-JSON document object's top-level `external_access` into the chunk rows it produces (first-write path), and writes the union snapshot onto the entity rows it merges (recomputed across batches in the merge step).
- GRAPH_ACL_PUSH extends to three write targets: the Neo4j entity union (as today, updated first), the affected document's chunk rows, and the affected entities' row snapshots — the latter via a recompute chain (graph reverse-lookup of affected entities → gather contributing documents' ACLs → OR-merge → batch upsert). Chunk-row updates go through a LightRAG-internal endpoint (see design.md for the channel decision).
- `rebuild_vdb.py` passthrough keeps the ACL fields present on rebuilt payloads of both collections; the collections are recreated via rebuild (no ARRAY auto-migration support needed — no historical data adaptation by decision).
- `QueryRequest` on all three endpoints accepts optional `user_emails[]` / `user_group_ids[]`; identity travels via ContextVar and is excluded from `QueryParam` mapping.
- Chunk filtering: engine `expr` on vector recall (`acl_is_public == true OR ARRAY_CONTAINS_ANY(emails, $) OR ARRAY_CONTAINS_ANY(groups, $)`); the source_id fallback path is filtered by a Milvus query (`id in [...] AND <acl expr>`) so the visibility decision stays in exactly one place (the expr).
- Entity filtering: engine `expr` on entity recall (same expr shape against the union snapshot — the seat layer); the hook re-verifies each entity against the graph's union properties (the authority layer, in-memory from the already-fetched node data).
- Relationship filtering: the hook anchors each relation's `source_id` chunk lineage into the collapse query — visible iff at least one lineage chunk is visible (stricter and better aligned with the description's source documents than endpoint-based judgment).
- Fail-closed end to end: no identity → public-only rows; null/missing ACL fields → invisible.
- Onyx query side: `search_graph` forwards the requesting user's identity in the payload.

## Non-goals

- No ACL fields on the Redis `text_chunks` records — the KV layer stays untouched; the source_id fallback path's decision is collapsed back into Milvus.
- No historical-data migration or ARRAY auto-migration support — pre-existing chunks/entities collections are recreated via `rebuild_vdb.py` (new project, no legacy data by decision).
- No changes to `base.py`; `QueryParam` is unchanged (identity rides ContextVar, not the param object).
- No `biz_id`-whitelist (document-set) filtering — visibility is judged by user/group against inline snapshots and graph union properties only.
- No ACL columns on the Milvus `relationships` collection — relations are judged by anchoring their existing `source_id` into the chunk collapse query.
- No change to the official WebUI or magicbox eval panels' request shape — they simply degrade to public-only results when identity is omitted.

## Impact

- `lightrag/kg/milvus_impl.py` — schema fields for chunks and entities collections, manifest registrations, index branches, one ContextVar-read hook in `query` (all additive, `fork-custom` marked).
- `lightrag/lightrag.py` — one ACL extraction helper next to `_extract_biz_doc_id`; union-snapshot wiring in the `ainsert_custom_kg` entity payload (additive, one call into the new union module).
- `lightrag/operate.py` — one ~10-line filter hook after `_merge_all_chunks`, plus one union-snapshot call in the `merge_nodes_and_edges` vdb_data construction (deviation from the zero-touch principle; see design.md "Filtering hook" and "Merge-clean contract").
- `lightrag/api/routers/s3_routes.py` — fork-owned file; flattens `external_access` into chunk payloads.
- `lightrag/api/routers/query_routes.py` — `QueryRequest` two optional fields, `to_query_params` exclusion, ContextVar setup in the three handlers.
- New modules — identity ContextVar, ACL expr builder, filter implementation, entity union recompute module, internal ACL-update endpoint (fork router).
- `lightrag/tools/rebuild_vdb.py` — passthrough defaults for both collections.
- Onyx — `graph_knowledge_search.py` payload identity, identity threading in the search pipeline, GRAPH_ACL_PUSH extension (three write targets). All in fork-owned directories except existing integration points.
