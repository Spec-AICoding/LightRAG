# Repository Guidelines

## Language Convention

- Always respond in 中文 (Chinese) — every session, every task, regardless of the
  user's input language or the language of the code being discussed.
- Technical identifiers (code, field names, file paths, log output) stay in their
  original form, but all explanation around them must be written in Chinese.
- Repository artifacts (code comments, log messages, commit messages) are written
  in English — see *Code Style* below.

## Project Overview

LightRAG: a graph-based RAG framework — extracts entities/relations into a
knowledge graph, queried via 5 modes (`local`, `global`, `hybrid`, `mix`,
`naive`). Top-level: `lightrag/` (core package), `lightrag_webui/` (React 19 +
TS on Bun + Vite), `scripts/` (`test.sh` runner; `setup/` wizard — use
`make env-*`, never call `setup.sh` directly), `tests/` (mirrors `lightrag/`);
datasets in `inputs/`, `rag_storage/`, `temp/`; deployment in `docs/`,
`k8s-deploy/`, compose files.

## Core Architecture

- Mixin chain: `LightRAG (@final) → _RoleLLMMixin → _StorageMigrationMixin →
  _PipelineMixin`. The layering is internal — not a subclassing surface; public
  API unchanged. `ainsert_custom_kg`, `_insert_done`, `_process_extract_entities`,
  `_refresh_addon_params_cache`, and `addon_params` accessors stay on `LightRAG`.
- 4 pluggable storage types: **KV** (LLM cache / chunks / doc info), **VECTOR**
  (embeddings), **GRAPH** (entity-relation), **DOC_STATUS**. The `workspace`
  param isolates data (file subdirs / collection prefixes / column filter /
  Qdrant payload partitioning).
- Per-file responsibilities → memory **Module Layout (lightrag 模块职责明细)**;
  `tests/` layout rules → memory **Development & Test Commands (开发与测试命令速查)**;
  usage code examples → memory **Core API Usage Patterns (核心 API 使用模式)**.

### Deep contracts (workspace memories — retrieve BEFORE touching related code)

- **Pipeline Concurrency Contract** → before modifying `lightrag/pipeline.py`,
  `lightrag/kg/pipeline_ingress.py`, `lightrag/kg/shared_storage.py`, or any
  enqueue/scan/clear-delete logic.
- **Purge Recovery Contract** → before modifying `_purge_kg_contributions`,
  `rebuild_knowledge_from_chunks`, `merge_nodes_and_edges`,
  `lightrag/tools/kg_integrity_repair.py`, or doc-status metadata whitelists.
- **Relation Weight Contract** → before changing relation write behavior in
  `lightrag/operate.py` or graph REST routes (sync docstrings + REST docs +
  `ProgramingWithCore.md` + custom-KG examples).

## Commands & Testing

Full command reference and the `tests/` directory mapping live in memory
**Development & Test Commands (开发与测试命令速查)**. Per-turn essentials:

- Setup: `uv sync [--extra api]`; server: `lightrag-server` /
  `uvicorn lightrag.api.lightrag_server:app --reload` / `lightrag-gunicorn`;
  WebUI (in `lightrag_webui/`): `bun run dev|build|lint`, tests via `bun test`
  (Bun built-in runner, NOT Vitest/Jest).
- Tests (pytest): **run only the directories mirroring the changed modules**
  via `./scripts/test.sh tests/api/config` — the suite is ~7000 tests (>6 min),
  so full runs only at milestones or cross-cutting changes (`base.py`,
  `utils.py`, `kg/shared_storage.py`); CI covers the rest. Report the subset
  you ran plus its pass count.
- New tests go in the mirrored subdir; new backends/providers get
  `tests/{kg,llm}/<name>_impl/` plus an empty `__init__.py` (the `_impl`
  suffix avoids `sys.path` shadowing).
- Mock external services (Redis, httpx, ...) — no live services in unit tests;
  add a regression test for every bug fix. Lint: `ruff check .`.

## Critical Pitfalls

- **Always `await rag.initialize_storages()` after instantiating `LightRAG`** —
  forgetting it manifests as `AttributeError: __aenter__` or
  `KeyError: 'history_messages'`.
- Switching embedding models requires clearing the data directory (optionally
  keeping `kv_store_llm_response_cache.json`) — old vectors won't match the new
  model's space.
- Custom embedding funcs must wrap via `@wrap_embedding_func_with_attrs` and
  call `.func` on the underlying (already-decorated functions cannot be
  re-wrapped). Full examples for init / storage / insert / query → memory
  **Core API Usage Patterns (核心 API 使用模式)**.

## Frontend Debugging via Playwright

For WebUI bugs whose symptoms only surface in the rendered DOM, drive the dev
server (`http://localhost:5173`) with the `document-skills:webapp-testing`
skill instead of reasoning from source alone. Seed state via `localStorage`
(persist key `settings-storage`, schema in
`lightrag_webui/src/stores/settings.ts`); use `wait_until="domcontentloaded"`
plus a selector wait — Vite dev polling makes `networkidle` time out.

## Configuration

`.env` is the API-server config (generate via `make env-base` or copy
`env.example`): server settings, storage connection strings, query params,
rerank (`RERANK_BINDING`/`RERANK_MODEL`), auth (`AUTH_ACCOUNTS`,
`LIGHTRAG_API_KEY`). Wizard rules: keep `.env` host-usable (container-only
hostnames and staged SSL paths stay in the compose layer);
`docker-compose.final.yml` is generated output from `scripts/setup/templates/`;
prefer `make env-*` targets over calling `scripts/setup/setup.sh` directly.

## Code Style

- Repo artifacts (comments, backend code, log messages, commit messages) in
  English; frontend i18n via i18next.
- Python: PEP 8, 4-space indent, type annotations, dataclasses for state,
  `lightrag.utils.logger` instead of print, async/await throughout.
- TS/React: functional components + hooks, PascalCase components, 2-space +
  single quotes (`@stylistic`), Tailwind utility-first; ESLint: TS-ESLint +
  React Hooks + Prettier (`no-explicit-any` allowed).

## Commit and Pull Request Guidance

- This repo is a fork of `HKUDS/LightRAG` — target PRs to `HKUDS/LightRAG`,
  not the fork itself.
- PR description: summary, motivation, linked issues, what changed, what broke
  and how it works.
- Commit messages (subject and body) in English — repository artifacts, like
  code comments and log messages, not conversational replies.
