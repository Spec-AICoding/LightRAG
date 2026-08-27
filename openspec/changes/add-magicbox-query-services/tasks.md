# Tasks: Magicbox Query Services

## 1. Backend skeleton

**Directory**: `magicbox/backend/` (new)

- [x] `pyproject.toml` — uv-managed, deps: `fastapi`, `uvicorn`, `neo4j`, `python-dotenv`, `pydantic`
- [x] `.env.example` — MAGICBOX_PORT, MAGICBOX_CORS_ORIGINS; NEO4J_* documented as inherited from root `.env`
- [x] `app/__init__.py`, `app/config.py`, `app/neo4j.py`, `app/main.py`, `app/routers/__init__.py`
- [x] `uv sync` succeeds in `magicbox/backend`
- [x] `python -c "from app.main import create_app"` imports cleanly

---

## 2. Config + Neo4j access module

**File**: `magicbox/backend/app/config.py`, `app/neo4j.py`

- [x] Load root `.env` (`../../.env`) then optional local `.env` override via `python-dotenv`
- [x] Resolve: NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD / NEO4J_DATABASE / NEO4J_WORKSPACE (default `base`)
- [x] Fail-fast DB check at startup: `SHOW DATABASES` — if NEO4J_DATABASE missing → raise (no silent default-DB fallback)
- [x] Async driver singleton (read-only sessions)
- [x] Workspace label helper with backtick doubling (mirror LightRAG escaping)
- [x] Unit-testable label escaping (e.g. label containing backtick)

---

## 3. Query endpoints

**File**: `magicbox/backend/app/routers/entities.py`, `app/routers/subgraph.py`

### 3a. `GET /health`

- [x] Returns `{status: "ok", database: ..., label: ..., entity_count: N}`

### 3b. `GET /entities/search`

- [x] Params: `property`, `value`, `limit` (default 20, cap 100)
- [x] Property whitelist: biz_id(list) / biz_name(list) → `IN` membership; entity_id / entity_type / description / file_path / source_id (string) → `CONTAINS`; others → 400
- [x] Cypher uses parameterized `n[$prop]` with whitelist-chosen match branch (no injection)
- [x] Returns `{ total: N, entities: [properties(n), ...] }`
- [x] Empty result → `{total: 0, entities: []}`

### 3c. `GET /entities/{entity_id}`

- [x] `MATCH (n:\`{label}\` {entity_id: $id}) RETURN properties(n)`; missing → 404
- [x] entity_id URL-encoded path (backticks/unicode safe)

### 3d. `GET /subgraph`

- [x] Params: `biz_id` (required), `max_nodes` (default 50), `max_relations` (default 100)
- [x] Seed: `MATCH (n:\`{label}\`) WHERE $biz_id IN n.biz_id`; expand 1 hop `-[r:DIRECTED]-`
- [x] Returns WebUI-compatible shape `{ nodes: [{id, label, ...props}], edges: [{source, target, ...props}] }`
- [x] No seed match → `{nodes: [], edges: []}`
- [x] Limits enforced in Cypher (`LIMIT` + collected map slicing)

### 3e. Wire-up

- [x] `app/main.py` `create_app()`: CORS (MAGICBOX_CORS_ORIGINS), mount routers, no auth
- [x] Startup: config load + fail-fast DB check before serving

---

## 4. Backend verification

- [x] `uv run uvicorn app.main:app --port 9622` starts; `/health` green against the running Neo4j (default `neo4j` db per current `.env`)
- [x] `GET /entities/search?property=biz_id&value=<known id>` returns the expected entities (current graph has 69 base nodes)
- [x] Whitelist rejection: unknown property → 400
- [x] `GET /subgraph?biz_id=<known id>` returns nodes/edges JSON
- [x] Read-only confirmed: no write Cypher anywhere in the service

---

## 5. Frontend parity clone

**Directory**: `magicbox/webui/` (new)

- [x] Copy `lightrag_webui/` source (src/, public/, config files) into `magicbox/webui/`; own `package.json` / `bun.lock`
- [x] `vite.config.ts`: dev port `5174`; proxy `/magicbox` → `http://localhost:9622`, rest → `http://localhost:9621`
- [x] New `src/api/magicbox.ts` client: `searchEntities(property, value)`, `getEntity(id)`, `getSubgraph(bizId, ...)`, base URL via `import.meta.env`
- [x] `env.development.smaple` / `env.local.sample` updated with the two backend base URLs
- [x] `bun install` succeeds; `bun run dev` serves the full UI
- [x] Parity check: login → documents page → graph page → chat page all functional against the official server
- [x] `bun run build` succeeds

---

## 6. Final acceptance

- [x] `openspec/changes/add-magicbox-query-services/` artifacts consistent (proposal / design / tasks)
- [x] No upstream files modified (`git status` clean outside `magicbox/` and `openspec/`)
- [x] Manual flow: start official server (9621) + magicbox backend (9622) + magicbox webui (5174); standard features work via 9621; property search & subgraph work via 9622
