# Tasks: Connector-Source Labeling and ACL Filtering

Execution order: ① gateway → ② ingestion injection → ③ magicbox query API → ④ webui → ⑤ end-to-end verification. Each phase is independently verifiable.

---

## 1. Onyx gateway: connector metadata endpoint

**Directory**: `/Users/wangpanfeng/work/workspace/product/onyx/magicbox-backend/` (untracked standalone service)

- [x] `app/main.py`: add `GET /connectors/{cc_pair_id}` — synchronous SQLAlchemy query joining `connector_credential_pair` ⋈ `connector`; returns `{cc_pair_id, name, source}`; `HTTPException(404)` when missing
- [x] Pydantic response model in the same file (style-consistent with existing models)
- [x] Verify: start service (`.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8090`), then `curl http://127.0.0.1:8090/connectors/4` → `{cc_pair_id: 4, name: "jira-connector2", source: "JIRA"}` and `/connectors/999` → 404

---

## 2. LightRAG ingestion: connector + ACL injection (`s3_routes.py`)

**File**: `lightrag/api/routers/s3_routes.py` (the only modified tracked file)

- [x] Filename parser: regex `^iab_(\d+)_\d+_\d+(?:_\d+)?\.json$` → `cc_pair_id` (optional trailing dedup suffix tolerates downloader renames); non-matching names return `None` (skip labeling)
- [x] Gateway client helper: httpx GET `{MAGICBOX_CONNECTOR_GATEWAY or http://127.0.0.1:8090}/connectors/{id}`, 3s timeout, module-level dict cache keyed by cc_pair_id; any exception → `None` (caller skips)
- [x] `_inject_connector_into_entities(rag, doc_id, chunk_texts, source, connector_name)`: UNWIND-pairs MATCH by `source_id CONTAINS p.chunk_id`; `SET n:\`{escaped_source}\`` (charset `^[A-Z][A-Z0-9_]*$` + backtick escape) plus `connector_source` / `connector_name` properties; sync Neo4j write via `asyncio.to_thread` (same as `_inject_field_into_entities`)
- [x] `_inject_acl_into_entities(rag, doc_id, chunk_texts)`: per object parse `external_access` (skip `None`); pairs carry `{is_public, emails, group_ids}`; Cypher accumulates: `is_public = coalesce(n.is_public, false) OR p.is_public`, emails/group_ids union-merge with `CASE WHEN x IN acc` dedup
- [x] Wire both into `_ingest_json_as_custom_chunks` after the `semantic_identifier` block; each in its own try/except with warning log (never blocks ingestion); extend the final log JSON with connector / ACL stats fields
- [x] Verify: `python3 -m py_compile lightrag/api/routers/s3_routes.py`; ingest one new file end-to-end (phase 5)

---

## 3. Magicbox backend: filter params + connector list + whitelist

**Files**: `magicbox/backend/app/routers/subgraph.py`, `app/routers/entities.py`

- [x] `subgraph.py`: add optional query params `connector` (charset-validated, escaped into `MATCH (n:\`base\`:\`{connector}\`)`), `user_email`, `groups` (comma-separated), `public_only`; ACL predicate appended to the seed WHERE (`n.is_public OR $email IN n.external_user_emails OR any(g IN $groups WHERE g IN n.external_user_group_ids)`, `public_only` narrows to `is_public` only); keep `biz_id` seeding and 1-hop DIRECTED expansion unchanged; caps raised to 2000/4000 to match the WebUI max-nodes ceiling
- [x] `subgraph.py`: add `GET /connectors` — `MATCH (n:\`base\`) WHERE n.connector_source IS NOT NULL RETURN DISTINCT n.connector_source, count(n)` → `[{source, entity_count}]`
- [x] `entities.py`: extend `PROPERTY_WHITELIST` with `connector_source` (string), `connector_name` (string), `is_public` (bool), `external_user_emails` (list), `external_user_group_ids` (list)
- [x] Verify: restart magicbox backend; `curl "/connectors"`; `curl "/subgraph?connector=JIRA"`; `curl "/subgraph?user_email=x@y.com"`; `curl "/subgraph?public_only=true"`; entity search by a new property

---

## 4. Magicbox webui: filter bar + data-source switch

**Files**: `magicbox/webui/src/api/magicbox.ts`, `magicbox/webui/src/features/GraphViewer.tsx` (plus any small components under `components/graph/`)

- [x] `magicbox.ts`: extend `getSubgraph` signature with `connector` / `userEmail` / `groups` / `publicOnly`; add `getConnectors`
- [x] GraphViewer filter bar: connector dropdown (from `getConnectors`), public-only checkbox, email / groups inputs
- [x] Data-source switch: any filter active → fetch and render magicbox `/subgraph` payload (existing sigma/graphology pipeline reused); no filter → existing official `/graphs` flow untouched
- [x] Verify: tsc + eslint clean, production build OK, browser check — dropdown lists JIRA (68), selecting it renders the connector subgraph, public-only narrows it (68→48 nodes), no error toasts

---

## 5. End-to-end verification

- [x] Restart onyx magicbox-backend (8090), LightRAG server (9621), magicbox backend (9622), webui
- [x] Ingest a fresh `iab_*.json` batch file; confirm log JSON shows connector / ACL stats
- [x] Neo4j spot check: entities of the new file carry the connector label, `connector_source` / `connector_name`, and ACL properties
- [x] UI: connector dropdown shows the new source; selecting it renders the connector-scoped subgraph; permission filter narrows it further
- [x] Failure drills: stop 8090 → ingestion completes with warn-only skip; non-`iab_` filename → no labeling, no error
