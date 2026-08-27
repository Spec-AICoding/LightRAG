# Design: Magicbox Query Services

## Architecture

```
┌──────────────────────┐         ┌────────────────────────────────┐
│  magicbox/webui       │         │  LightRAG Server (官方, 9621)   │
│  (React, dev 5174)    │───────▶│  · 标准功能(chat/documents/graph)│
│  · 复刻官方 WebUI      │         │  · from-s3 / biz_id 注入(不变)  │
│  · 标准功能 → 官方服务  │         └───────────────┬────────────────┘
│  · 定制查询 → magicbox │                          │
└──────────┬───────────┘         ┌────────────────▼────────────────┐
           │                     │  magicbox/backend (FastAPI, 9622)│
           └───────────────────▶│  · 只读 Neo4j 检索查询            │
                                 │  · 无认证(内网服务)               │
                                 └───────────────┬────────────────┘
                                                 │ async driver, read-only
                                        ┌────────▼────────┐
                                        │ Neo4j (共享 .env)│
                                        └─────────────────┘
```

Both backends read the same `.env` (root) for Neo4j connection settings. The magicbox backend is **read-only**: it never writes entities, relations, or properties.

## Backend (`magicbox/backend`)

### Stack

- Python 3.12+, FastAPI + uvicorn, managed by uv (`magicbox/backend/pyproject.toml`)
- `neo4j` async driver (same package LightRAG uses)
- **No LightRAG import** — pure driver access; the only shared knowledge is the storage schema, documented below as a contract

### Configuration

Loaded with `python-dotenv` from the **root `.env`** (path `../../.env` relative to the backend), with optional `magicbox/backend/.env` override:

| Var | Usage |
|---|---|
| `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` | driver connection |
| `NEO4J_DATABASE` | target database, **fail-fast**: if the database does not exist, startup errors instead of silently falling back to the server default (lesson from the biz_id injection incident) |
| `NEO4J_WORKSPACE` | workspace label, default `base` (mirrors LightRAG resolution) |
| `MAGICBOX_PORT` | default `9622` |
| `MAGICBOX_CORS_ORIGINS` | default `http://localhost:5174` |

Label escaping follows LightRAG: backticks with internal backticks doubled.

### Endpoints (all GET, no auth)

| Endpoint | Description |
|---|---|
| `/health` | liveness + Neo4j connectivity check |
| `/entities/search?property=&value=&limit=` | Property-whitelisted entity search |
| `/entities/{entity_id}` | Single entity detail (full property map) |
| `/subgraph?biz_id=&max_nodes=&max_relations=` | Subgraph seeded by biz_id, 1-hop expansion |

**Property whitelist** (reject anything else — prevents arbitrary property access):

| Property | Type | Match |
|---|---|---|
| `biz_id` | list | `$value IN n[$prop]` (exact membership) |
| `biz_name` | list | `$value IN n[$prop]` |
| `entity_id` | string | `CONTAINS` |
| `entity_type` | string | `CONTAINS` |
| `description` | string | `CONTAINS` |
| `file_path` | string | `CONTAINS` |
| `source_id` | string | `CONTAINS` |

Cypher sketch (search):

```cypher
MATCH (n:`base`)
WHERE $value IN n[$prop]            -- list property
   OR n[$prop] CONTAINS $value      -- string property (branch chosen by whitelist entry)
RETURN properties(n) AS node
ORDER BY n.entity_id ASC
LIMIT $limit
```

Cypher sketch (subgraph): seed = entities whose `biz_id` contains the given id; expand 1 hop across `DIRECTED` relations; enforce `max_nodes` / `max_relations` (default 50 / 100); return `{ nodes: [...], edges: [{source, target, ...props}] }` in the same shape the WebUI graph store consumes.

### Schema contract (documented assumptions)

- Node label: workspace label (default `` `base` ``)
- Node properties: `entity_id`, `entity_type`, `description`, `source_id`, `file_path`, `created_at`, plus custom arrays `biz_id`, `biz_name` (written by the from-s3 injection flow)
- Relations: type `DIRECTED`, properties `source_id`, `description`, `keywords`, `weight`, `file_path`, `created_at`
- If upstream changes these, the read-only service fails loudly (no data corruption possible) — re-verify after each upstream upgrade

## Frontend (`magicbox/webui`)

- Copy of `lightrag_webui` source into `magicbox/webui` with its **own** `package.json`, lockfile, and build — upstream updates to `lightrag_webui/` never touch this directory
- Stack unchanged: React 19 + TypeScript + Vite + Tailwind + Bun
- API split:
  - official endpoints (auth, documents, query, graph CRUD) → official server base URL (default `http://localhost:9621`)
  - magicbox query endpoints → magicbox backend base URL (default `http://localhost:9622`)
  - new small client module `src/api/magicbox.ts` alongside the existing `src/api/lightrag.ts`
- Vite dev proxy: `/magicbox` → `9622`, everything else → `9621` (dev server port `5174` to avoid clashing with `lightrag_webui`'s `5173`)
- V1 = parity clone: all official pages work unchanged; no custom pages yet

## Risks

| Risk | Mitigation |
|---|---|
| Upstream schema drift (entity/relation property changes) | Read-only service fails loudly; schema contract section above; verify after upgrades |
| No auth on magicbox backend | Bind to internal network / localhost in deployment; documented as internal service |
| Port collisions | Reserved: official `9621`, magicbox `9622`, webui dev `5174` |
| Duplicate label-resolution logic vs LightRAG | Tiny (env default + backtick escape); covered by `/health` and a startup self-check |

## Directory layout

```
magicbox/
├── backend/
│   ├── pyproject.toml
│   ├── .env.example
│   └── app/
│       ├── main.py          # create_app, no auth, CORS
│       ├── config.py        # root .env loading, fail-fast DB check
│       ├── neo4j.py         # driver, label resolution, escaping
│       └── routers/
│           ├── entities.py  # /entities/search, /entities/{id}
│           └── subgraph.py  # /subgraph
└── webui/                   # parity clone of lightrag_webui
```
