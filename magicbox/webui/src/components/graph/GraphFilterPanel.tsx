import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Filter, ShieldCheck, Tags, X } from 'lucide-react'

import { getConnectors, OnyxConnectorInfo } from '@/api/onyx'
import { getSubgraphDocuments, type SubgraphDocument } from '@/api/magicbox'
import { useSettingsStore } from '@/stores/settings'
import { useGraphStore } from '@/stores/graph'
import GraphLabels from '@/components/graph/GraphLabels'

/**
 * Top-left graph filter panel: three aligned rows — subject (labels +
 * search), connector (source → instance), permission (ACL).
 *
 * ALL filter widgets (connector source/instance, public-only, email, groups)
 * are DRAFTS: changing them does not query anything. The Apply button commits
 * every draft to the settings store at once and forces a re-fetch via
 * incrementGraphDataVersion() — even when the values are identical to the
 * last applied filter, so pressing Apply always re-queries. Clear resets all
 * dimensions back to the unfiltered view.
 *
 * The subject row (node labels + search) stays immediate: it drives the
 * official /graphs data source, not the deferred /subgraph one.
 */
interface GraphFilterPanelProps {
  /** GraphSearch element (needs Sigma context; injected by GraphViewer). */
  searchSlot?: ReactNode
}

const GraphFilterPanel = ({ searchSlot }: GraphFilterPanelProps) => {
  const { t } = useTranslation()
  const graphFilter = useSettingsStore.use.graphFilter()
  const setGraphFilter = useSettingsStore.use.setGraphFilter()
  const isFetching = useGraphStore.use.isFetching()

  // --- Drafts: initialized from the persisted filter once on mount ---
  const [connectorDraft, setConnectorDraft] = useState(graphFilter.connector)
  const [connectorNameDraft, setConnectorNameDraft] = useState(graphFilter.connectorName)
  const [docBizIdDraft, setDocBizIdDraft] = useState(graphFilter.bizId)
  const [visibilityDraft, setVisibilityDraft] = useState<'all' | 'public' | 'non_public'>(
    graphFilter.publicOnly ? 'public' : graphFilter.nonPublicOnly ? 'non_public' : 'all'
  )
  const [emailDraft, setEmailDraft] = useState(graphFilter.userEmail)
  const [groupsDraft, setGroupsDraft] = useState(graphFilter.groups)

  // --- Connector list (onyx gateway) with bounded retry ---
  const [connectors, setConnectors] = useState<OnyxConnectorInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [loadFailed, setLoadFailed] = useState(false)
  const mountedRef = useRef(true)

  // Three attempts with 1s/2s backoff: the gateway is a separate service, and
  // a single transient failure would otherwise leave the dropdown empty until
  // the page is reloaded. State updates only happen after awaits, so the mount
  // effect below stays clear of react-hooks/set-state-in-effect.
  const loadConnectors = useCallback(async () => {
    try {
      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          const res = await getConnectors()
          if (mountedRef.current) setConnectors(res.connectors ?? [])
          return
        } catch (err) {
          console.error(`Failed to load connectors (attempt ${attempt}):`, err)
          if (attempt < 3) {
            await new Promise((resolve) => setTimeout(resolve, 1000 * attempt))
          }
        }
      }
      if (mountedRef.current) setLoadFailed(true)
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void loadConnectors()
    return () => {
      mountedRef.current = false
    }
  }, [loadConnectors])

  // Instances of the currently drafted source (cascading options).
  const instances = useMemo(
    () => connectors.find((c) => c.source === connectorDraft)?.names ?? [],
    [connectors, connectorDraft]
  )

  // --- Documents (biz_id) of the drafted instance, loaded from Neo4j via
  // /subgraph/documents. Display name prefers semantic_identifier; distinct
  // documents sharing a name stay separate (dedup key is biz_id). Bounded
  // retry like the connector list; a sequence counter drops stale responses
  // when the user switches instances quickly. ---
  const [documents, setDocuments] = useState<SubgraphDocument[]>([])
  const [docLoading, setDocLoading] = useState(false)
  const [docLoadFailed, setDocLoadFailed] = useState(false)
  const docRequestSeqRef = useRef(0)

  const loadDocuments = useCallback(async (connectorName: string) => {
    const seq = ++docRequestSeqRef.current
    // NOTE: the caller sets docLoading before invoking (event handlers do it
    // directly); no synchronous setState here — the mount effect calls this
    // function and react-hooks/set-state-in-effect forbids sync updates.
    try {
      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          const res = await getSubgraphDocuments(undefined, connectorName)
          if (mountedRef.current && seq === docRequestSeqRef.current) {
            setDocuments(res.documents ?? [])
            setDocLoadFailed(false)
          }
          return
        } catch (err) {
          console.error(
            `Failed to load documents for instance '${connectorName}' (attempt ${attempt}):`,
            err
          )
          if (attempt < 3) {
            await new Promise((resolve) => setTimeout(resolve, 1000 * attempt))
          }
        }
      }
      if (mountedRef.current && seq === docRequestSeqRef.current) {
        setDocLoadFailed(true)
      }
    } finally {
      if (mountedRef.current && seq === docRequestSeqRef.current) {
        setDocLoading(false)
      }
    }
  }, [])

  // Reload the document list whenever the drafted instance changes. The
  // reset of the drafted document is done in the change handlers below, so
  // this effect only fetches.
  useEffect(() => {
    if (!connectorNameDraft) return
    void loadDocuments(connectorNameDraft)
  }, [connectorNameDraft, loadDocuments])

  // Selecting a new source resets the drafted instance AND document: the
  // previously picked values may not belong to the new source.
  const onSourceChange = (source: string) => {
    setConnectorDraft(source)
    setConnectorNameDraft('')
    setDocBizIdDraft('')
    setDocuments([])
    setDocLoadFailed(false)
    setDocLoading(false)
  }

  // Selecting a new instance resets the drafted document: it belongs to the
  // previous instance's document list. The document list itself is loaded by
  // the effect on connectorNameDraft below (single fetch path — no direct
  // loadDocuments call here to avoid a duplicate request).
  const onInstanceChange = (name: string) => {
    setConnectorNameDraft(name)
    setDocBizIdDraft('')
    setDocuments([])
    setDocLoadFailed(false)
    setDocLoading(!!name)
  }

  // Commit every draft at once and force a re-fetch. Bumping the data version
  // changes the fetch signature, so Apply always re-queries even when the
  // applied values are unchanged (this also recovers from a previously failed
  // query whose signature had been suppressed).
  const apply = () => {
    setGraphFilter({
      connector: connectorDraft,
      connectorName: connectorNameDraft,
      bizId: docBizIdDraft,
      publicOnly: visibilityDraft === 'public',
      nonPublicOnly: visibilityDraft === 'non_public',
      userEmail: emailDraft.trim(),
      groups: groupsDraft.trim()
    })
    useGraphStore.getState().incrementGraphDataVersion()
  }

  const clear = () => {
    setConnectorDraft('')
    setConnectorNameDraft('')
    setDocBizIdDraft('')
    setDocuments([])
    setDocLoadFailed(false)
    setVisibilityDraft('all')
    setEmailDraft('')
    setGroupsDraft('')
    setGraphFilter({
      connector: '',
      connectorName: '',
      bizId: '',
      publicOnly: false,
      nonPublicOnly: false,
      userEmail: '',
      groups: ''
    })
    useGraphStore.getState().incrementGraphDataVersion()
  }

  // Whether a filter has been APPLIED (drives the clear button visibility).
  const filterApplied = !!(
    graphFilter.connector ||
    graphFilter.connectorName ||
    graphFilter.bizId ||
    graphFilter.userEmail ||
    graphFilter.groups ||
    graphFilter.publicOnly ||
    graphFilter.nonPublicOnly
  )

  return (
    <div className="bg-background/60 absolute top-2 left-2 z-10 flex flex-col items-stretch rounded-xl border-2 backdrop-blur-lg">
      {/* Row 1: subject filter — immediate, drives the /graphs data source */}
      <div className="flex items-center gap-2 px-3 py-1.5">
        <span className="text-muted-foreground flex w-20 shrink-0 items-center gap-1 text-xs font-medium whitespace-nowrap">
          <Tags size={14} />
          {t('graphPanel.subjectFilter.title')}
        </span>
        <GraphLabels />
        {searchSlot}
      </div>

      {/* Row 2: connector filter — deferred drafts */}
      <div className="flex items-center gap-2 border-t px-3 py-1.5">
        <span className="text-muted-foreground flex w-20 shrink-0 items-center gap-1 text-xs font-medium whitespace-nowrap">
          <Filter size={14} />
          {t('graphPanel.connectorFilter.title')}
        </span>

        <select
          className="bg-background text-foreground h-8 max-w-48 rounded-md border px-2 text-xs outline-none"
          value={connectorDraft}
          onChange={(e) => onSourceChange(e.target.value)}
          title={
            loadFailed
              ? t('graphPanel.connectorFilter.loadFailedTitle')
              : t('graphPanel.connectorFilter.sourceTitle')
          }
        >
          <option value="">
            {loading
              ? t('graphPanel.connectorFilter.loading')
              : loadFailed
                ? t('graphPanel.connectorFilter.loadFailedOption')
                : t('graphPanel.connectorFilter.allConnectors')}
          </option>
          {!loadFailed &&
            connectors.map((c) => (
              <option key={c.source} value={c.source}>
                {c.source}
              </option>
            ))}
        </select>
        {loadFailed && (
          <button
            className="text-primary hover:underline text-xs whitespace-nowrap"
            onClick={() => {
              setLoading(true)
              setLoadFailed(false)
              void loadConnectors()
            }}
          >
            {t('graphPanel.connectorFilter.retry')}
          </button>
        )}

        <select
          className="bg-background text-foreground h-8 max-w-56 rounded-md border px-2 text-xs outline-none disabled:opacity-50"
          value={connectorNameDraft}
          onChange={(e) => onInstanceChange(e.target.value)}
          disabled={!connectorDraft || instances.length === 0}
          title={t('graphPanel.connectorFilter.instanceTitle')}
        >
          <option value="">{t('graphPanel.connectorFilter.allInstances')}</option>
          {instances.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>

        <select
          className="bg-background text-foreground h-8 max-w-64 rounded-md border px-2 text-xs outline-none disabled:opacity-50"
          value={docBizIdDraft}
          onChange={(e) => setDocBizIdDraft(e.target.value)}
          disabled={!connectorNameDraft || documents.length === 0}
          title={t('graphPanel.connectorFilter.documentTitle')}
        >
          <option value="">
            {docLoading
              ? t('graphPanel.connectorFilter.loading')
              : docLoadFailed
                ? t('graphPanel.connectorFilter.loadDocumentsFailed')
                : t('graphPanel.connectorFilter.allDocuments')}
          </option>
          {!docLoadFailed &&
            documents.map((doc) => (
              // title shows the raw biz_id so same-name documents can be told apart
              <option key={doc.biz_id} value={doc.biz_id} title={doc.biz_id}>
                {doc.name}
              </option>
            ))}
        </select>
        {docLoadFailed && connectorNameDraft && (
          <button
            className="text-primary hover:underline text-xs whitespace-nowrap"
            onClick={() => {
              setDocLoading(true)
              void loadDocuments(connectorNameDraft)
            }}
          >
            {t('graphPanel.connectorFilter.retry')}
          </button>
        )}
      </div>

      {/* Row 3: permission filter — deferred drafts + Apply/Clear */}
      <div className="flex items-center gap-2 border-t px-3 py-1.5">
        <span className="text-muted-foreground flex w-20 shrink-0 items-center gap-1 text-xs font-medium whitespace-nowrap">
          <ShieldCheck size={14} />
          {t('graphPanel.permissionFilter.title')}
        </span>

        <select
          className="bg-background text-foreground h-8 rounded-md border px-2 text-xs outline-none"
          value={visibilityDraft}
          onChange={(e) =>
            setVisibilityDraft(e.target.value as 'all' | 'public' | 'non_public')
          }
          title={t('graphPanel.permissionFilter.visibilityTitle')}
        >
          <option value="all">{t('graphPanel.permissionFilter.visibilityAll')}</option>
          <option value="public">{t('graphPanel.permissionFilter.publicOnly')}</option>
          <option value="non_public">{t('graphPanel.permissionFilter.nonPublicOnly')}</option>
        </select>

        <input
          className="bg-background text-foreground h-8 w-36 rounded-md border px-2 text-xs outline-none"
          placeholder={t('graphPanel.permissionFilter.userEmail')}
          value={emailDraft}
          onChange={(e) => setEmailDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') apply()
          }}
        />
        <input
          className="bg-background text-foreground h-8 w-44 rounded-md border px-2 text-xs outline-none"
          placeholder={t('graphPanel.permissionFilter.groups')}
          value={groupsDraft}
          onChange={(e) => setGroupsDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') apply()
          }}
        />

        <button
          className="bg-primary text-primary-foreground h-8 rounded-md px-3 text-xs font-medium disabled:opacity-50"
          onClick={apply}
          disabled={isFetching}
        >
          {t('graphPanel.permissionFilter.apply')}
        </button>
        {filterApplied && (
          <button
            className="text-muted-foreground hover:text-foreground flex h-8 w-8 items-center justify-center rounded-md border"
            onClick={clear}
            title={t('graphPanel.permissionFilter.clear')}
          >
            <X size={14} />
          </button>
        )}
      </div>
    </div>
  )
}

export default GraphFilterPanel
