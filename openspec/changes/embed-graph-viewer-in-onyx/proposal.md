# Embed Graph Viewer in Onyx (Embedding Mode + URL-driven Document Linkage)

## Summary

Make the magicbox webui embeddable so the Onyx magicbox-web can host the Knowledge Graph view inside an iframe as part of a unified, single-sign-on interface. Three capabilities are added, all opt-in via URL parameters so standalone usage is unchanged:

1. **Embedding mode** — `?embed=1&tab=knowledge-graph` renders only the `GraphViewer` (no `SiteHeader`, no tab bar, no status indicators), so the app can live inside an Onyx iframe without duplicating Onyx's own navigation and document/retrieval features.
2. **Runtime config override** — URL parameters (`apiPrefix`, `magicboxPrefix`, etc.) take precedence over `window.__LIGHTRAG_CONFIG__`, so the embedded app can point its API calls at the Onyx same-origin proxy instead of the dev-local ports.
3. **Document linkage** — `?doc=<link>` (the Onyx document `link`, which equals the magicbox `biz_id`) auto-applies the document filter and loads that document's subgraph on startup; optional `connector` / `connectorName` parameters prefill the remaining filters.

Authentication is removed at the configuration level (comment out `AUTH_ACCOUNTS` in `.env`) — the server then reports `auth_configured: false` and issues guest tokens, which the existing frontend logic already handles. No auth code is deleted.

## Motivation

The user wants a single entry point with unified login for both products: Onyx (connector/document management, search) as the host, and the LightRAG knowledge graph as an analysis view under a new admin-only "Knowledge Governance" sidebar section in Onyx. The graph view is currently a standalone app on its own port with its own login; embedding it into Onyx requires (a) a chrome-free rendering mode, (b) an API base that works inside the Onyx origin, and (c) a way for Onyx pages to deep-link into a specific document's subgraph.

## Goals

- Magicbox webui (`magicbox/webui`): `App.tsx` reads URL params; when `embed=1` + `tab=knowledge-graph` it renders `<GraphViewer />` full-screen, skipping the Tabs layout, header, status indicator, and API-key alert.
- `lib/constants.ts` (or equivalent config bootstrap): URL params override `window.__LIGHTRAG_CONFIG__` for API prefixes (LightRAG API, magicbox API) and the webui base.
- Graph initialization from URL: `doc` param maps to the existing document filter (`biz_id` semantics) and triggers the existing subgraph query path; `connector` / `connectorName` params prefill the other filter rows.
- Deployment config (`.env`, not code): `AUTH_ACCOUNTS` commented out so the backend runs unauthenticated; frontend guest-token fallback keeps working (verified existing logic in `api/lightrag.ts`).
- Standalone behavior unchanged: without `embed` params the app renders exactly as today.

## Non-goals

- No changes to the official LightRAG WebUI (`lightrag_webui/`) or to LightRAG core (`lightrag/` Python package) — embedding-mode changes live entirely in `magicbox/webui` plus `.env`.
- No code removal of the authentication machinery (login page, token interceptor) — auth is disabled by configuration only, so it can be re-enabled independently.
- No reverse linkage (graph → Onyx document) and no two-way iframe messaging protocol in this change; linkage is one-way via URL parameters.
- No real SSO integration — "unified login" means the user authenticates only with Onyx; LightRAG runs unauthenticated behind the Onyx-origin proxy.
- No multi-page Vite build; embedding mode is a runtime branch, not a second bundle.
