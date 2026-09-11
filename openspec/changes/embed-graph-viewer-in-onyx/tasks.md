# Tasks: Embed Graph Viewer in Onyx

## Implementation

- [ ] `magicbox/webui/src/lib/constants.ts`: overlay URL search params (`apiPrefix`, `magicboxPrefix`, `onyxPrefix`, `webuiPrefix`) over `window.__LIGHTRAG_CONFIG__` in the config bootstrap
- [ ] `magicbox/webui/src/App.tsx`: embed branch — when `embed=1` and `tab=knowledge-graph`, render `<GraphViewer />` full-screen inside `<main>`, skipping `SiteHeader`, `StatusIndicator`, `ApiKeyAlert`, and the Tabs layout; keep `ThemeProvider` / `TabVisibilityProvider`
- [ ] Graph filter initialization: on `GraphViewer` mount in embed mode, read `doc` / `connector` / `connectorName` URL params and apply them to the graph filter store state, reusing the existing deferred-query path so exactly one subgraph fetch fires
- [ ] Verify: `bunx tsc --noEmit` clean; `bun run build` produces the same `dist/` layout as before

## Configuration

- [ ] `.env`: comment out `AUTH_ACCOUNTS` (backend then runs unauthenticated; guest-token fallback in `api/lightrag.ts` verified)
- [ ] Restart the LightRAG server (9621) and confirm `/auth-status` returns `auth_configured: false`

## Verification

- [ ] Standalone regression: open `http://localhost:5174` with no params — full Tabs UI renders, graph loads, expansion and label normalization behave as before
- [ ] Embed smoke test in dev: `http://localhost:5174/?embed=1&tab=knowledge-graph` — only the graph renders, no header/tab bar
- [ ] Linkage smoke test in dev: `http://localhost:5174/?embed=1&tab=knowledge-graph&doc=<a known biz_id URL>` — document filter auto-applied and subgraph loads
- [ ] Config override check in dev: `?embed=1&tab=knowledge-graph&magicboxPrefix=/magicbox&apiPrefix=/lightrag-api` with Vite proxies in place (or temporarily point at the Onyx dev proxy) — requests hit the prefixed paths
- [ ] Full end-to-end with the Onyx host once the Onyx change lands: login Onyx → 知识治理 → 知识图谱 → iframe shows graph; open a document → 「在图谱中查看」 → correct subgraph
