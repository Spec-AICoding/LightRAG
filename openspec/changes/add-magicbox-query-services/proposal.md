# Add Magicbox Query Services

## Summary

Add a `magicbox/` directory at the repo root containing two independent services for customization, keeping them fully isolated from upstream LightRAG code so upstream updates continue to merge cleanly:

1. `magicbox/backend` — a lightweight FastAPI service providing graph retrieval queries against the shared Neo4j (property-based entity search, biz_id subgraph retrieval, entity detail). No auth. No ingestion.
2. `magicbox/webui` — an independent React frontend replicated from `lightrag_webui`, initially at feature parity, whose API calls target both the official LightRAG server (standard features) and the magicbox backend (custom query features).

## Motivation

Custom requirements keep growing (biz_id / biz_name injection, property-based entity query, and future permission-filtered graph retrieval). Currently these are embedded in the LightRAG repo (`lightrag/api/routers/s3_routes.py`), which conflicts with the goal of tracking upstream. The two services isolate all future custom development in one dedicated directory that upstream never touches, so LightRAG and its WebUI remain merge-clean.

## Goals

- `magicbox/backend`: FastAPI service (Python + uv, same stack as LightRAG) with read-only Neo4j graph retrieval endpoints:
  - property-based entity search with a property whitelist
  - subgraph retrieval seeded by biz_id
  - single-entity detail lookup
  - health check
- `magicbox/webui`: full parity clone of `lightrag_webui` (React 19 + TypeScript + Vite + Tailwind + Bun), independent build, API calls split between the official server and the magicbox backend
- Zero modification to upstream files (`lightrag/`, `lightrag_webui/`); `s3_routes.py` and everything else stay untouched
- No authentication on the magicbox backend (internal service)

## Non-goals

- Not migrating `s3_routes.py` or any ingestion logic out of the LightRAG server
- Not running any pipeline / ingestion / document processing in the magicbox backend (query-only)
- Not removing `s3_routes.py` from the LightRAG repo
- Not adding custom UI pages yet (parity first; custom pages come in a later change)
- Not proxying RAG chat queries (`aquery`) — standard features stay on the official server
