# Add Connector-Source Labeling and ACL Filtering to the Knowledge Graph

## Summary

Enrich the knowledge graph with two kinds of document-origin metadata at S3 ingestion time, and expose both as filters in the Knowledge Graph UI:

1. **Connector source** — during JSON ingestion (`iab_{cc_pair_id}_{attempt}_{batch}.json`), parse the `cc_pair_id` from the file name, resolve the connector name/source through the onyx `magicbox-backend` (a read-only PG gateway), and stamp every extracted entity with the source as a native Neo4j label plus `connector_source` / `connector_name` properties.
2. **Document ACL** — inject each JSON object's `external_access` (`is_public`, `external_user_emails`, `external_user_group_ids`) into the same entities as accumulated properties (union semantics).

The magicbox backend (`/subgraph`) and the magicbox webui then gain connector / permission filtering so the UI can retrieve "the whole subgraph under one connector" or "entities visible to a given user".

## Motivation

The Knowledge Graph page currently shows the whole deduplicated graph with no way to scope retrieval by data origin or by permission. Connector metadata lives in the Onyx PostgreSQL (`connector_credential_pair` ⋈ `connector`); per-document ACL lives inside each batch JSON file (`external_access`). Users want to answer questions like "show me the JIRA subgraph" or "show me what user x@y.com can see" directly from the graph UI.

## Goals

- Onyx `magicbox-backend` (standalone, read-only): new `GET /connectors/{cc_pair_id}` returning `{cc_pair_id, name, source}` (404 when missing).
- LightRAG `s3_routes.py` (the only modified tracked file): filename parsing, gateway lookup with in-process cache, and two post-hoc, failure-tolerant injections:
  - connector injection: `SET n:\`{source}\`` (native label, charset-validated + escaped) plus `connector_source` / `connector_name` properties
  - ACL injection: `is_public` (bool, OR accumulation) and `external_user_emails` / `external_user_group_ids` (lists, union accumulation)
- Magicbox backend (`magicbox/backend`): `/subgraph` accepts optional `connector` / `user_email` / `groups` filters; new `GET /connectors` returns the distinct connector sources present in the graph; entity-search property whitelist extended with the five new properties.
- Magicbox webui: connector dropdown plus permission filter (public / email / groups); when filters are active the graph renders from magicbox `/subgraph`, otherwise the existing official `/graphs` view is unchanged.
- Zero modification to Onyx core (`backend/onyx/*`) and to LightRAG core (`lightrag/lightrag.py`, `pipeline.py`, `operate.py`, `kg/*`, `lightrag_webui/`).

## Non-goals

- No backfill of already-ingested documents (only newly ingested files get labels / ACL).
- No chunk-level ACL and no `Document` nodes in the graph — ACL is flattened onto entities with union semantics (loses per-document provenance).
- No hard access control: filtering is a UI scoping mechanism for an internal, unauthenticated service, not a security boundary.
- No changes to the official `/graphs` endpoint or the official WebUI.
