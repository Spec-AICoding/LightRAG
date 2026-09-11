# Design: Embed Graph Viewer in Onyx

## 1. Embedding Mode Detection and Rendering

`App.tsx` currently renders a fixed Tabs layout (`documents` / `knowledge-graph` / `retrieval` / `api`) with `SiteHeader`, `StatusIndicator`, and `ApiKeyAlert`. In embedding mode this chrome is skipped entirely.

**Detection**: read `window.location.search` once at mount (`URLSearchParams`):
- `embed=1` and `tab=knowledge-graph` → render `<GraphViewer />` directly inside the existing `<main className="flex h-screen w-screen overflow-hidden">` wrapper, with `ThemeProvider` + `TabVisibilityProvider` kept (theme is needed; tab visibility is harmless).
- Any other combination → current behavior, byte-for-byte unchanged.

**Why a runtime branch, not a second Vite entry**: one bundle, one build pipeline, and the standalone app keeps all its features. The branch is ~15 lines in `App.tsx`.

**What is skipped in embed mode**: `SiteHeader` (tab bar), `StatusIndicator`, `ApiKeyAlert`, `useBackendState` message toast logic. `ApiKeyAlert` is safe to skip because the backend runs unauthenticated (`auth_configured: false` → guest tokens, existing fallback in `api/lightrag.ts`).

## 2. Runtime Config Override (URL params > `window.__LIGHTRAG_CONFIG__`)

`lib/constants.ts` builds `SiteInfo` / API prefixes from `window.__LIGHTRAG_CONFIG__` (injected by the FastAPI server in production, or by the Vite dev plugin). Inside the Onyx iframe there is no FastAPI injection — the iframe src is a plain static `index.html` served from Onyx's `public/`. Therefore:

- In the config bootstrap, after reading `window.__LIGHTRAG_CONFIG__`, overlay URL search params:
  - `apiPrefix` → LightRAG API prefix (used by `api/lightrag.ts`)
  - `magicboxPrefix` → magicbox backend prefix (used by `api/magicbox.ts`, e.g. `/magicbox`)
  - `onyxPrefix` → onyx gateway prefix (used by `api/onyx.ts`, e.g. `/onyx`) — only if the embed surface needs it (graph view does not, but the override mechanism is uniform)
  - `webuiPrefix` → base for relative links (defaults to `apiPrefix + "/webui/"` formula or empty in embed mode)
- Values must match the Onyx proxy paths (`/lightrag-api`, `/magicbox`) chosen in the Onyx change `add-lightrag-graph-integration`.

**Cleanup on unmount**: none needed — URL params are read once at bootstrap.

## 3. Document Linkage (`?doc=<link>` → biz_id subgraph)

The Onyx document `link` (e.g. `https://wpf0310.atlassian.net/browse/SCRUM-5`) equals the magicbox `biz_id` stored on graph entities. Existing machinery (verified): `GraphFilterPanel` document filter + `useLightragGraph` subgraph query path.

**Wiring** (embed mode only):
1. On `GraphViewer` mount, read `doc` / `connector` / `connectorName` from URL params.
2. If `doc` present: set the document-filter state (the same store field the panel writes — `useGraphStore` / settings) and trigger the same subgraph fetch the panel triggers on selection. Must reuse the existing "deferred filter execution" convention so the query fires exactly once after all params are set.
3. `connector` / `connectorName` params prefill the connector filter row; the existing cascading filter logic (connector → document dropdown) applies — i.e. the document dropdown options derive from the connector.
4. No polling, no postMessage; iframe refresh re-reads URL params and restores the same state (deep-linkable).

**Edge cases**:
- `doc` present but not found in the graph → show the graph with the filter applied and empty result (existing empty-state), not an error screen.
- No params → graph renders with its defaults (same as today's standalone first-load).

## 4. Authentication Off (Configuration Only)

- `.env`: comment out `AUTH_ACCOUNTS`. Verified: `auth_configured = bool(auth_handler.accounts)` in `lightrag/api/lightrag_server.py`; with no accounts the server returns `auth_configured: false` + a guest `access_token`; the frontend interceptor already uses it and never shows the login gate.
- Backend restart required (`kill <pid>`; relaunch via `magicbox/backend`-style nohup — actually the LightRAG server on 9621; exact command in repo docs).
- `LIGHTRAG_API_KEY` stays unset (it is an additional auth path; leaving it unset keeps the server open to the Onyx-origin proxy).
- Security note: this is acceptable only because the LightRAG API will be reachable exclusively through the Onyx origin (same-origin proxy) in the integrated deployment; standalone access on 9621 becomes open too — accepted for this internal deployment.

## 5. Verified Compatibility Facts (from exploration)

- LightRAG server sets no `X-Frame-Options` / CSP `frame-ancestors` — iframe embedding works with zero backend change.
- `GraphViewer` is self-contained (sigma container, filter panel, properties view all internal) — safe to render without the Tabs layout.
- The webui build uses relative `base: './'` — the same `dist/` can be copied under Onyx `public/lightrag-ui/` and asset URLs resolve correctly.

## 6. Files Touched

- `magicbox/webui/src/App.tsx` — embed branch (skip chrome, render `GraphViewer`)
- `magicbox/webui/src/lib/constants.ts` — URL-param config overlay (may be a small new helper instead; keep the diff minimal)
- `magicbox/webui/src/features/GraphViewer.tsx` and/or the graph filter store — URL-param → filter-state initialization
- `.env` — comment out `AUTH_ACCOUNTS` (deployment config, not code)
