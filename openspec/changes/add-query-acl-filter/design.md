# Design: Query API ACL Filtering

## Architecture

```
QueryRequest {query, mode, ..., user_emails[], user_group_ids[]}   (all three endpoints)
        │
        ▼
Handler: set identity ContextVar ──▶ rag.aquery* / aquery_data
        │
        ▼
kg_query → _build_query_context
  ├─ Stage 1  _perform_kg_search
  │    ├─ entities_vdb.query(ll_keywords)   ──▶ EXPR (union snapshot — seat filter)  ②′
  │    ├─ relationships_vdb.query(hl_keywords)   (no expr — judged in the hook)
  │    └─ chunks_vdb.query(query)           ──▶ EXPR FILTER (engine, prefilter)      ①
  ├─ Stage 3  _merge_all_chunks
  │    ├─ vector_chunks          (already filtered at ①)
  │    ├─ entity_chunks:  source_id → KV get_by_ids   ──▶ ② collapse query
  │    └─ relation_chunks: source_id → KV get_by_ids  ──▶ ②
  └─ Stage 3.5 FILTER HOOK (new, ContextVar callback)  ──▶ ②③
       ├─ ② chunk lineage: one Milvus collapse query (id in [...] AND expr)
       ├─ ③ entities: authoritative re-verification vs graph union props (in-memory)
       └─ ③ relations: source_id anchored into the same collapse query
  ├─ Stage 2/4 truncate, build context, (LLM for /query & /query/stream)
```

## Two judgment layers

Access decisions deliberately split into a **seat layer** and an **authority layer** — the split is what makes the entities row snapshot safe to keep stale:

| Layer | Data | Job | Cost of staleness |
|---|---|---|---|
| Seat | row ACL snapshots (chunks ①, entities ②′) | keep invisible items out of `top_k` at recall time | efficiency only — wasted seats, never a leak |
| Authority | graph union props + chunks collapse query (hook ②③) | final visibility judgment | safety — push updates the graph first and chunks rows directly; both settle fastest |

Because the seat layer never decides safety, the entities union snapshot needs **best-effort sync only** — no strict cross-store reconciliation. A stale entity snapshot degrades recall-seat efficiency (the hook drops the item anyway), not security. This layering is what makes the entities-column decision affordable: its worst case is the previous post-recall-only design, and its best case is a clean `top_k` in multi-tenant deployments.

Decision points:

| Point | Object | Decision location |
|---|---|---|
| ① Vector chunk recall | chunks | Milvus search `expr` — prefilter, top_k from the visible set |
| ②′ Entity recall | entities | Milvus search `expr` on the row union snapshot — seat filter |
| ② Chunk lineage (source_id fallback) | chunks | one Milvus collapse query `id in [...] AND <acl expr>` — visible subset |
| ③ Entities | graph nodes | hook: authoritative union-ACL judgment on the fetched node data (in-memory, zero extra queries) |
| ③ Relations | graph edges | hook: `source_id` lineage anchored into the ② collapse query |

## Data architecture: snapshot on the row

The authoritative ACL source is Onyx PostgreSQL (perm-sync four-tuple). Data flows to the decision points at **write time**, never at read time:

- **First write**: batch-JSON document objects carry top-level `external_access`; ingestion flattens it into every chunk row it produces (`ainsert_custom_chunks` path, extraction helper next to `_extract_biz_doc_id`), and writes the union snapshot onto entity rows during the merge step.
- **Permission change**: GRAPH_ACL_PUSH updates three targets in order — Neo4j entity union (first, it feeds the authority layer), the affected document's chunk rows, then the affected entities' row snapshots via a recompute chain (see "Entity union recompute" below).
- **Read**: expr on the rows plus the in-memory authority judgment; no cross-service fetch on the query path.

Role split on the chunks row:

- `biz_doc_id` — identity key. Always accurate; used by Onyx-side mapping, audits, and snapshot rebuilding. Not used for the visibility judgment itself.
- `acl_*` fields — judgment snapshot. Serves filtering only; allowed to lag briefly behind PG.

Role split on the entities row:

- `source_id` — chunk lineage (unchanged, maintained by merge/purge as today).
- `acl_*` fields — **union snapshot** of all contributing documents' ACLs. Serves the seat filter only; lag is acceptable and self-healing (see the two-layer table above).

### Entity union snapshot semantics

An entity accumulates contributions from multiple documents (union accumulation, mirroring the graph `biz_id` semantics). Its row snapshot is the OR-merge of every contributing document's ACL:

- `acl_is_public = OR(contributing docs' is_public)`
- `acl_external_user_emails = ∪(contributing docs' emails)`
- `acl_external_user_group_ids = ∪(contributing docs' groups)`

Maintained at two write sites: the ingestion merge step (union of the new batch's document ACL with the row's existing snapshot) and the push recompute chain (full recompute from the graph's `biz_id` list — see below). The union is deliberately **not** reversible (V(A) ∪ V(B) cannot recover V(B)); judgment granularity stays document-level, so this is not a problem for either layer.

### Why inline snapshot instead of a reference-style ACL table

The repo has a reference-style ACL convention ("store only the ownership key, compute the allow-list at query time") whose stated purpose is to avoid write amplification on permission change. This change deliberately stores the snapshot inline, and the calculus is recorded here rather than silently bypassed:

- Change frequency is document-granular and low (page permission edits, page moves); one change rewrites the document's few dozen chunk rows. Group-membership changes rewrite **nothing** (rows store which groups may see the document; the judgment matches against the requester's groups).
- The snapshot removes the entire independent-ACL-source cost: no separate KV table, no allow-list computation, no cache invalidation, no cross-store alignment window between the table and the rows.
- The convention is revised in scope, not repealed: for high-frequency × high-row-count ACLs, the reference style remains the default. The boundary goes into the reference-style spec.

## Identity transport

- `QueryRequest` gains optional multi-valued `user_emails[]` / `user_group_ids[]`. They are **excluded** in `to_query_params` (the dump would otherwise fail `QueryParam(**data)` on unknown fields).
- A new module owns an identity ContextVar (`acl_identity = (emails, groups)`); the three handlers set it per request. `milvus_impl.query` and the filter hook read it. `QueryParam`/`base.py` stay untouched — this is what keeps the upstream merge surface minimal.

## Decision semantics

### ① Chunk expr (engine prefilter)

```
acl_is_public == true
  or ARRAY_CONTAINS_ANY(acl_external_user_emails, $emails)
  or ARRAY_CONTAINS_ANY(acl_external_user_group_ids, $groups)
```

- Built by a dedicated expr-builder module; `milvus_impl.query` appends it when the ContextVar carries an identity (additive `fork-custom` block).
- Fail-closed at the schema level: nullable columns, so missing/null ACLs evaluate false in the expr → invisible.
- No identity in ContextVar → expr reduces to `acl_is_public == true` (public-only), the natural fail-closed behavior. This is also what the magicbox eval panel gets without changes.

### ②′ Entity expr (seat filter)

The same expr shape against the entities row union snapshot, appended to `entities_vdb.query` in the same `milvus_impl.query` block. It is a **seat filter**: it may use a stale snapshot (see two-layer table). The authority judgment in the hook re-verifies every recalled entity regardless.

### ② Chunk lineage collapse query

The KV records carry no ACL fields (Redis untouched). After `_merge_all_chunks`, the filter hook takes the merged chunk ids **plus every recalled relation's `source_id` chunks** and issues one Milvus query `id in [...] AND <acl expr>`; the returned ids are the visible subset. The visibility judgment therefore lives in exactly one place — the expr — and the hook only computes set differences. Chunks missing from Milvus (storage inconsistency) are invisible (fail-closed).

### ③ Entity / relationship authoritative judgment

- **Entity**: Neo4j already returns the union ACL properties on every node fetched (`is_public`, `external_user_emails`, `external_user_group_ids`, union semantics since `add-connector-acl-graph-filtering`). The hook marks an entity visible when `is_public OR emails ∩ requester ≠ ∅ OR groups ∩ requester ≠ ∅`. Zero extra queries — the node data is already in memory from retrieval. This, not the row snapshot, is the safety judgment; push updates the graph first so this settles fastest.
- **Relationship**: visible iff its `source_id` lineage contains at least one visible chunk in the ② collapse result — judgment anchored to the description's source documents. This is stricter than endpoint-based judgment (a relation whose endpoints are visible but whose description was extracted from an invisible document is correctly hidden) and unifies the relation judgment into the same collapse query.
- Accepted union residue: an entity's merged description may contain sentences contributed by documents the requester cannot see — inherent to union accumulation, already accepted by the graph-viewing feature.

## Filtering hook (the deliberate operate.py deviation)

The zero-touch principle for `operate.py`/`base.py` holds for `base.py` but cannot fully hold for `operate.py` once `/query` and `/query/stream` are in scope: their LLM consumes the context built inside `_build_query_context`, so a wrapper *after* `aquery` returns would already have leaked invisible content into the prompt. All three endpoints converge on `_build_query_context`, so one hook covers them:

- After `_merge_all_chunks` (Stage 3), invoke a ContextVar-registered filter callback when set (~10 additive lines, `fork-custom (query-acl)` marked). Upstream behavior is unchanged when the ContextVar is empty (default).
- The callback implements ② and ③ and returns the filtered entities/relations/chunks before truncation — the correct timing (post-recall, pre-rerank/truncate).
- `naive_query` needs no hook: its only source is the chunks VDB, already filtered by ①. (`bypass` mode does no retrieval.)

### Recall-headroom expansion

`top_k` seats are allocated inside the engine. The entities recall is now seat-filtered (②′), but the relationships recall is **not** (no ACL fields on the relationships collection — the judgment needs data not on the row). The handler wrapper therefore still expands `top_k` (fixed factor, e.g. 2×) before calling `aquery*`, accepting the modest extra recall cost instead of re-implementing truncation. Residual: under extreme invisible ratios the visible relation count may stay below the requested headroom — documented, not silently "fixed".

## Update channel (GRAPH_ACL_PUSH → Milvus rows)

Two candidates for the chunk-row update:

| | A: direct Milvus from Onyx worker | B: LightRAG-internal endpoint (chosen) |
|---|---|---|
| Coupling | Onyx pulls in pymilvus + connection config | explicit HTTP contract (matches the repo's push/pull principle) |
| Schema ownership | field names/expr knowledge duplicated in Onyx | stays with milvus_impl, single owner |
| Consistency with rebuild/manifest logic | bypasses it | reuses it |
| New surface | none | one fork-router endpoint (`/acl/chunks` update by `biz_doc_id`, combined_auth protected) |

Decision: **B**. The endpoint takes `{biz_doc_id, is_public, emails, groups}`, resolves affected chunk ids via `biz_doc_id == $doc` query, and upserts the ACL fields only.

The entity-row update follows the same channel: a second internal endpoint (or the same, split by concern) runs the **recompute chain** for the affected document's entities:

1. Graph reverse-lookup: entities whose `biz_id` list contains the changed document.
2. Gather each entity's full `biz_id` list; fetch the ACL of each contributing document (one Milvus chunks query on `biz_doc_id in [...]`, any row per document — same-document rows share the snapshot).
3. OR-merge into the union snapshot; batch-upsert the entities rows' three ACL fields only.

Best-effort semantics: a failure here degrades seat efficiency, never safety (two-layer table). Update order across targets — Neo4j union first, then chunk rows, then entity rows — keeps the authority layer ahead of the seat layer at every moment.

## Evaluated and rejected alternatives

These were evaluated during design discussions and rejected; the record is kept so they are not re-argued:

| Alternative | Form | Rejected for |
|---|---|---|
| C-original: entities union-ACL columns as the *safety* judgment | row snapshot decides visibility, no hook re-verification | double state vs Neo4j union requires strict reconciliation; a stale snapshot becomes a security window |
| Entity recall filtered by chunk-lineage expr (`ARRAY_CONTAINS_ANY(source_id, $chunk_ids)`) | no new columns, judgment borrowed from chunks rows | Milvus expr has no cross-collection join; the visible-chunk set is corpus-wide and cannot fit an expr parameter; the query-recall chunk set ≠ the entity lineage set (massive false drops) |
| is_public split ("public docs need no enumeration, enumerate only private-visible ids") | shrink the parameter set by a public/private split | the private-visible id set is still user-scale (thousands+); precomputing `has_public_source` on entity rows cannot express "visible to *this* requester" — recall-level fail-closed and correctness are mutually exclusive when the judgment depends on the requester |
| E: `biz_doc_ids` on entity rows, filtered by a precomputed visible-document set | document-level ids instead of chunk-level | doc set must be the **corpus-wide visible set** (a query-recall-derived set is a relevance set, not a permission set — mass false drops); the full set costs a per-query DISTINCT scan; caching it introduces a permission-narrowing leak window; plus a second lineage field to maintain |

The pattern: every route that moves the judgment into the recall engine requires materializing some judgment input onto the entity row, and every materialization (union snapshot, lineage ids, ownership ids) costs more than the recall-seat benefit it buys — except the union snapshot **when demoted to seat-only duty**, which is the design chosen here.

## Merge-clean contract

Per-file upstream-collision risk (assessed against this fork's current merge practice; LR2-style ongoing merges are the norm):

| File | Change | Risk |
|---|---|---|
| `operate.py` | ~10-line hook after `_merge_all_chunks`; one union-snapshot call in the merge vdb_data construction | **low-med — two touchpoints; the merge-construction call sits in an upstream-active area, so it MUST stay a one-line call into the new union module (re-anchor on upstream refactor)** |
| `base.py` | none | none |
| `milvus_impl.py` | schema/manifest/index additions (chunks + entities) + ContextVar read in `query` | low — additive blocks, `fork-custom` marked; upstream edits there target pooling/concurrency |
| `query_routes.py` | two optional fields + exclusion + ContextVar setup | low — independent Field blocks |
| `lightrag.py` | one extraction helper next to `_extract_biz_doc_id`; one union-snapshot call in the `ainsert_custom_kg` entity payload | low-med — same one-line-call discipline as operate.py |
| `s3_routes.py`, new modules, fork router | fork-owned / new files | none |

Upstream-merge checklist (mandatory after each upstream sync):

1. Re-run the ACL regression subset: `tests/test_acl_expr.py`, `tests/test_acl_utils.py`, `tests/test_acl_filter.py`, `tests/test_acl_identity.py`, `tests/test_acl_rebuild_passthrough.py`, `tests/kg/milvus_impl/test_acl_seat_filter.py`, `tests/api/routes/test_acl_routes.py` — chunk expr prefilter, ②′ entity seat filter, ② collapse query, ③ authority judgment (entity union + relation lineage), fail-closed (no identity → public-only).
2. Verify the `operate.py` hook is still positioned after chunk merging and before truncation; verify the union-snapshot calls still sit at the vdb_data construction sites (operate.py merge, lightrag.py custom-kg).
3. If upstream added a new retrieval path (new query mode), check hook coverage — fail-closed defaults cover unknown paths only partially; new paths need explicit review.
4. `git diff --stat` sanity: no upstream file gained non-additive ACL code.

## Accepted residues (Consistency without transactions)

1. **Row ACL lags PG**: GRAPH_ACL_PUSH update is asynchronous; a failed update leaves the old snapshot in effect (stale-permission window in either direction). Heals on the next push replay; direction documented per failure.
2. **Entity seat snapshot lags the graph**: the recompute chain runs after the Neo4j update; a stale or failed entity-row update wastes recall seats but never leaks — the hook re-verifies against the graph union (authority layer). Heals on the next push replay.
3. **Neo4j union vs chunk-row snapshot timing**: entity filtering (union props) and chunk filtering (row snapshot) can briefly disagree after a permission change until both stores settle. Directional, heals on push replay.
4. **Recall-headroom shrink** (see above): visible relations may fall below requested `top_k` under extreme invisible ratios (relation recall is not seat-filtered). Accepted; not compensated beyond the fixed expansion factor.
5. **KV has chunks Milvus lacks** (pre-existing inconsistency): ② treats them as invisible. Heals when the chunks collection is rebuilt.

## Verification prerequisites

- **URL normalization**: perm-sync `doc_id`, row `biz_doc_id`, entity `biz_id`, and the recompute chain's document lookup must inhabit the same URL space (case, trailing slash, page-id form). Verify against real data before wiring the push update.
- **Collections recreation**: ARRAY columns are outside the existing VARCHAR auto-migration mechanism; the chunks and entities collections are recreated via `rebuild_vdb.py` (accepted — no historical data).

## Magicbox eval panel behavior (documented change)

The magicbox WebUI (`magicbox/webui/src/api/lightrag.ts`, `RetrievalView`) sends no
`user_emails` / `user_group_ids` on `/query` and `/query/stream` — the guest eval panel
has no requester identity. After this change those requests hit the fail-closed path:
**identity omitted → public-only results** (rows without `acl_is_public == true` are
invisible in both the seat and authority layers). This is intended; per-user retrieval
from the panel would require threading Onyx session identity into the WebUI, which is
out of scope for this change.
