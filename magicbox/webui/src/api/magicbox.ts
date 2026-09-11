import axios from 'axios'
import type { LightragGraphType } from './lightrag'
import { onyxClient } from './onyx'

// Types — mirror the magicbox backend response shapes
// (magicbox/backend/app/routers/{entities,subgraph}.py).

export type MagicboxEntity = {
  entity_id: string
  entity_type?: string
  description?: string
  [key: string]: any
}

export type MagicboxEntitySearchResponse = {
  total: number
  entities: MagicboxEntity[]
}

export type MagicboxEntityDetailResponse = {
  entity: MagicboxEntity
}

// The backend's subgraph payload is shape-compatible with the WebUI graph
// type (nodes with id/labels/properties, edges with id/source/target/type).
export type MagicboxSubgraphResponse = LightragGraphType

export type SubgraphFilters = {
  connector?: string
  connectorName?: string
  bizId?: string
  userEmail?: string
  groups?: string
  publicOnly?: boolean
  nonPublicOnly?: boolean
}

export type SubgraphDocument = {
  biz_id: string
  name: string
  link?: string
  kg_stage?: string
  entities?: number
}

export type MagicboxHealthResponse = {
  status: string
  database: string
  label: string
  entity_count: number
}

// Dev: point straight at the magicbox backend (http://localhost:9622).
// Production build: fall back to the relative /magicbox prefix, which a
// reverse proxy routes to 9622 (see vite.config.ts dev proxy).
const magicboxBaseUrl = import.meta.env.VITE_MAGICBOX_BACKEND_URL || '/magicbox'

const magicboxClient = axios.create({
  baseURL: magicboxBaseUrl,
  headers: {
    'Content-Type': 'application/json'
  }
})

/**
 * Search entities by a whitelisted property.
 * List properties (biz_id, biz_name, semantic_identifier) match exact
 * membership; string properties (entity_id, entity_type, description,
 * file_path, source_id) match with CONTAINS.
 */
export const searchEntities = async (
  property: string,
  value: string,
  limit: number = 20
): Promise<MagicboxEntitySearchResponse> => {
  const response = await magicboxClient.get('/entities/search', {
    params: { property, value, limit }
  })
  return response.data
}

/** Fetch a single entity's full property map by entity_id. */
export const getEntity = async (
  entityId: string
): Promise<MagicboxEntityDetailResponse> => {
  const response = await magicboxClient.get(
    `/entities/${encodeURIComponent(entityId)}`
  )
  return response.data
}

/**
 * Fetch the 1-hop subgraph around entities carrying the given biz_id,
 * optionally narrowed by connector source and ACL filters.
 *
 * Either biz_id or filters.connector seeds the query (connector alone
 * returns the connector's whole graph, capped by maxNodes). ACL filters
 * are OR-ed with the entity's is_public flag.
 */
export const getSubgraph = async (
  bizId: string | null,
  maxNodes: number = 50,
  maxRelations: number = 100,
  filters: SubgraphFilters = {}
): Promise<MagicboxSubgraphResponse> => {
  const response = await magicboxClient.get('/subgraph', {
    params: {
      biz_id: bizId ?? filters.bizId ?? undefined,
      max_nodes: maxNodes,
      max_relations: maxRelations,
      connector: filters.connector ?? undefined,
      connector_name: filters.connectorName ?? undefined,
      user_email: filters.userEmail ?? undefined,
      groups: filters.groups ?? undefined,
      public_only: filters.publicOnly ? true : undefined,
      non_public_only: filters.nonPublicOnly ? true : undefined
    }
  })
  return response.data
}

/**
 * List the documents belonging to a connector instance.
 *
 * Served by the onyx gateway (8090) straight from the onyx config DB —
 * the document↔connector-instance attribution lives in
 * document_by_connector_credential_pair, which is authoritative. The old
 * /subgraph/documents path inferred this from Neo4j entity biz_id arrays,
 * which cross-contaminated when entities merged across connectors (e.g. a
 * person entity contributed by both JIRA and Confluence docs).
 */
export const getSubgraphDocuments = async (
  connector?: string,
  connectorName?: string
): Promise<{ documents: SubgraphDocument[] }> => {
  const response = await onyxClient.get('/connectors/documents', {
    params: {
      name: connectorName ?? undefined,
      source: connector ?? undefined
    }
  })
  return response.data
}

/** Liveness plus Neo4j connectivity and entity count. */
export const checkMagicboxHealth = async (): Promise<MagicboxHealthResponse> => {
  const response = await magicboxClient.get('/health')
  return response.data
}
