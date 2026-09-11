/**
 * Runtime path prefix configuration.
 *
 * The browser-visible URL prefixes (API base, WebUI mount path) used to be
 * baked into the bundle from `import.meta.env.VITE_*_PREFIX` at build time,
 * forcing one build per reverse-proxy mount point. They are now resolved at
 * REQUEST time: the FastAPI server replaces a `<!-- __LIGHTRAG_RUNTIME_CONFIG__ -->`
 * comment in `index.html` with a `<script>window.__LIGHTRAG_CONFIG__ = ...</script>`
 * snippet built from `LIGHTRAG_API_PREFIX` / `LIGHTRAG_WEBUI_PATH`. This module
 * is the single read point for that injected value.
 *
 * Dev parity: `vite.config.ts` performs the same injection via a
 * `transformIndexHtml` plugin using `VITE_DEV_API_PREFIX` /
 * `VITE_DEV_WEBUI_PREFIX`, so dev and prod use the same lookup.
 *
 * Always read through {@link normalizeApiPrefix} / {@link normalizeWebuiPrefix}
 * downstream (see `constants.ts`); this module returns the raw injected value.
 */

declare global {
  interface Window {
    __LIGHTRAG_CONFIG__?: {
      apiPrefix?: string
      webuiPrefix?: string
    }
  }
}

const config = (() => {
  const injected =
    (typeof window !== 'undefined' && window.__LIGHTRAG_CONFIG__) || {}

  // URL parameters take precedence over the injected config. This is how the
  // embedded build works: when served from the Onyx host's static files there
  // is no FastAPI injection, so the iframe URL carries apiPrefix (and
  // optionally webuiPrefix) explicitly, e.g.
  //   /lightrag-ui/?embed=1&tab=knowledge-graph&apiPrefix=/lightrag-api
  const params =
    typeof window !== 'undefined' && window.location?.search
      ? new URLSearchParams(window.location.search)
      : null

  return {
    apiPrefix: params?.get('apiPrefix') ?? injected.apiPrefix,
    webuiPrefix: params?.get('webuiPrefix') ?? injected.webuiPrefix
  }
})()

/** Browser-visible API prefix; empty string means same-origin / no prefix. */
export function getRuntimeApiPrefix(): string | undefined {
  return config.apiPrefix
}

/** Browser-visible WebUI mount path including the trailing slash. */
export function getRuntimeWebuiPrefix(): string | undefined {
  return config.webuiPrefix
}
