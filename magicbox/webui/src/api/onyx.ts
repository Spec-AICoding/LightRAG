import axios from 'axios'

// Onyx gateway (onyx magicbox-backend, port 8090): read-only PostgreSQL
// metadata service. Used for the connector configuration list — the graph
// data itself still comes from the magicbox backend 9622 (src/api/magicbox.ts).
//
// Dev: use the same-origin /onyx prefix; the Vite dev proxy (vite.config.ts)
// strips it and forwards to 8090. Production: route /onyx to the onyx gateway
// in your reverse proxy, or set VITE_ONYX_GATEWAY_URL to the gateway origin.
const onyxBaseUrl = import.meta.env.VITE_ONYX_GATEWAY_URL || '/onyx'

// Shared so api/magicbox.ts can reach gateway endpoints (document list)
// without duplicating the base-URL logic.
export const onyxClient = axios.create({
  baseURL: onyxBaseUrl,
  headers: {
    'Content-Type': 'application/json'
  }
})

/** One configured connector source with its instance names. */
export type OnyxConnectorInfo = {
  source: string
  names: string[]
}

export type OnyxConnectorsResponse = {
  connectors: OnyxConnectorInfo[]
}

/** List all configured connector sources and their instances (onyx config DB). */
export const getConnectors = async (): Promise<OnyxConnectorsResponse> => {
  const response = await onyxClient.get('/connectors')
  return response.data
}
