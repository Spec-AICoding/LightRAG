# Design: Connector-Source Labeling and ACL Filtering

## Architecture

```
┌─ Ingestion time (LightRAG server 9621) ────────────────────────────┐
│ iab_4_467_1.json                                                    │
│   │ filename regex ^iab_(\d+)_\d+_\d+\.json$  →  cc_pair_id = 4     │
│   │                                                                 │
│   ├─ httpx (3s timeout, in-process cache)                           │
│   │      └──▶ onyx magicbox-backend (8090)                          │
│   │              GET /connectors/4                                  │
│   │              PG: connector_credential_pair ⋈ connector          │
│   │              → {name: "jira-connector2", source: "JIRA"}        │
│   │                                                                 │
│   └─ post-hoc injection (after ainsert_custom_chunks, same         │
│      pattern as biz_id / semantic_identifier, each independently    │
│      failure-tolerant):                                             │
│      · _inject_connector_into_entities                              │
│      · _inject_acl_into_entities        (reads JSON external_access)│
└───────────────────────────────────────────────────────────────────┘
                                │ Neo4j writes
                                ▼
┌─ Query time (magicbox 9622 + webui) ───────────────────────────────┐
│ Graph UI filter bar: [connector ▾] [public ☑] [email] [groups]      │
│   filter active → GET /subgraph?connector=JIRA&user_email=...       │
│   no filter     → official /graphs (unchanged)                     │
│ GET /connectors → dropdown list (distinct connector_source)         │
└───────────────────────────────────────────────────────────────────┘
```

## Data model (Neo4j entity nodes)

| Property / label | Type | Semantics |
|---|---|---|
| `:`<source>`` (e.g. `:JIRA`) | native label | stable enum-like taxonomy (INGESTION_API / GMAIL / JIRA); used for `MATCH` seeding |
| `connector_source` | string | dual-written alongside the label; used for dropdown listing and property search |
| `connector_name` | string | user data (`jira-connector2`); property, NOT a label |
| `is_public` | bool | OR accumulation across all contributing documents |
| `external_user_emails` | list | union accumulation, deduped |
| `external_user_group_ids` | list | union accumulation, deduped |

The workspace label (`base`) stays the single LightRAG partition label; the connector label is additive and never used by LightRAG internals (MERGE/MATCH/delete all key on `base`), so upstream operations are unaffected.

## Shared-entity semantics (union accumulation)

Entities are deduplicated globally by name, so one node can be contributed by several documents with different ACLs. All ACL fields therefore accumulate rather than overwrite:

- emails / group ids: union with `CASE WHEN x IN acc` dedup (idempotent across re-ingestion)
- `is_public`: `coalesce(n.is_public, false) OR p.is_public`

Visibility rule for filtering: entity is visible iff `is_public = true OR user_email ∈ external_user_emails OR user_groups ∩ external_user_group_ids ≠ ∅`. This is a UI scoping mechanism, not a security boundary (see Non-goals).

## Injection mechanics (`s3_routes.py`)

Both injections reuse the existing pattern from `_inject_property_sync` / `_inject_field_into_entities`:

- Pairs are built from `make_custom_chunk_id(doc_id, chunk_text)` per JSON object.
- Cypher: `UNWIND $pairs AS p MATCH (n:\`base\`) WHERE n.source_id CONTAINS p.chunk_id ...`
- Connector label is interpolated into the Cypher only after charset validation (`^[A-Z][A-Z0-9_]*$`) and backtick escaping (label cannot be parameterized in Cypher).
- `external_access` may be `null` (verified: `iab_4_467_0/1.json`, `iab_3_226_0.json`) → objects without it are skipped.
- Each injection runs in its own try/except with a warning log; failure never blocks ingestion. Log JSON gains connector / ACL stats fields.

## Gateway contract

- Onyx `magicbox-backend` (untracked standalone service) adds `GET /connectors/{cc_pair_id}`: synchronous SQLAlchemy query, `SELECT c.name, c.source FROM connector_credential_pair ccp JOIN connector c ON c.id = ccp.connector_id WHERE ccp.id = :cc_pair_id`; 404 when absent. Startup table verification already covers both tables.
- LightRAG calls `{MAGICBOX_CONNECTOR_GATEWAY:-http://127.0.0.1:8090}/connectors/{id}` with a 3s timeout and an in-process dict cache keyed by cc_pair_id. Any failure → skip connector labeling for that file (warn only).

## Query API (magicbox backend)

- `GET /subgraph` gains optional params alongside the existing `biz_id` seed:
  - `connector`: seeds become `MATCH (n:\`base\`:\`{escaped}\`)` (charset-validated)
  - `user_email` / `groups`: optional ACL predicate appended to the seed `WHERE`
  - Expansion stays the existing 1-hop DIRECTED pattern; caps unchanged.
- `GET /connectors`: `MATCH (n:\`base\`) WHERE n.connector_source IS NOT NULL RETURN DISTINCT n.connector_source, count(n)` → dropdown data.
- `entities.py` `PROPERTY_WHITELIST` additions: `connector_source` (string), `connector_name` (string), `is_public` (bool), `external_user_emails` (list), `external_user_group_ids` (list).

## WebUI (magicbox/webui)

- `magicbox.ts`: extend `getSubgraph` with `connector` / `userEmail` / `groups`; add `getConnectors`.
- GraphViewer: a filter bar (connector dropdown from `GET /connectors`, public checkbox, email/group inputs). When any filter is active the graph data source switches to magicbox `/subgraph`; with no filters the existing official `/graphs` flow is untouched.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| 8090 gateway down at ingestion time | 3s timeout + cache + warn-only skip; ingestion unaffected; labels land on the next re-ingestion |
| Label injection via file-derived data | charset whitelist `^[A-Z][A-Z0-9_]*$` + backtick escaping; values come from PG `connector.source`, not the filename |
| Union ACL over-permissive for shared entities | accepted by design (UI scoping, not security); stricter per-chunk ACL explicitly out of scope |
| `s3_routes.py` is a tracked upstream file | all additions confined to this file + helpers; core modules untouched; single-file revert |
| Existing data lacks labels/ACL | accepted (no backfill); only newly ingested files carry the metadata |
