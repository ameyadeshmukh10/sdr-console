// Thin fetch wrappers around the Python backend. All paths are relative, so
// the Vite dev proxy (dev) and the Python static server (prod) both work.

// --- auth ------------------------------------------------------------------
const TOKEN_KEY = 'sdr_auth_token'

export function getToken() { return localStorage.getItem(TOKEN_KEY) }
export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

// Called when the server rejects our token (expired/cleared) on any request
// other than login itself — the app uses this to drop back to the login screen.
let onUnauthorized = null
export function setOnUnauthorized(fn) { onUnauthorized = fn }

// --- demo mode -------------------------------------------------------------
// The active demo profile id, or null for live data. Kept here (not only in React
// state) so every request picks it up from the single authHeaders() chokepoint —
// no endpoint can forget to pass it and silently show live data inside a demo.
const DEMO_KEY = 'sdr_demo_profile'

export function getDemoProfile() { return localStorage.getItem(DEMO_KEY) || null }
export function setDemoProfile(id) {
  if (id) localStorage.setItem(DEMO_KEY, id)
  else localStorage.removeItem(DEMO_KEY)
}

function authHeaders(extra) {
  const h = { ...(extra || {}) }
  const t = getToken()
  if (t) h.Authorization = 'Bearer ' + t
  const d = getDemoProfile()
  if (d) h['X-Demo-Profile'] = d
  return h
}

function handleUnauthorized(path) {
  // The login call expects a 401 on a bad password and shows its own error;
  // every other 401 means our session is dead, so bounce to the login screen.
  if (path === '/api/login') return
  setToken(null)
  if (onUnauthorized) onUnauthorized()
}

async function get(path) {
  const res = await fetch(path, { headers: authHeaders() })
  if (res.status === 401) handleUnauthorized(path)
  if (!res.ok) {
    let detail = ''
    try { detail = (await res.json()).error || '' } catch { /* ignore */ }
    // .status lets callers branch on the HTTP code (the message may be the
    // server's error text, e.g. "a sync is already running" on a 409).
    throw Object.assign(new Error(`${res.status} ${detail}`.trim()), { status: res.status })
  }
  return res.json()
}

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: body ? JSON.stringify(body) : undefined,
  })
  if (res.status === 401) handleUnauthorized(path)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw Object.assign(new Error(data.error || `${res.status}`), { status: res.status })
  return data
}

export const api = {
  login: (email, password) => post('/api/login', { email, password }),
  status: () => get('/api/status'),
  batches: (status, limit) => {
    const q = new URLSearchParams()
    if (status) q.set('status', status)
    if (limit) q.set('limit', limit)
    return get('/api/batches?' + q.toString())
  },
  orchestrationConfig: () => get('/api/orchestration/config'),
  analytics: () => get('/api/analytics'),
  refreshAnalytics: () => post('/api/analytics/refresh'),
  linkedinAnalytics: () => get('/api/analytics/linkedin'),
  aisdrAnalytics: () => get('/api/analytics/aisdr'),
  aisdrSyncStatus: () => get('/api/hubspot/aisdr/status'),
  aisdrSync: (opts) => post('/api/hubspot/aisdr/sync', opts || {}),
  // Unenrollment checker — suppression rules status + manual sweep ({dry_run?}).
  unenrollStatus: () => get('/api/unenroll/status'),
  unenrollRun: (opts) => post('/api/unenroll/run', opts || {}),
  outreach: (params) => get('/api/outreach?' + new URLSearchParams(params).toString()),
  outreachDetail: (id) => get('/api/outreach/' + encodeURIComponent(id)),
  // limit null = "Maximum" (the whole list); a number caps how many NEW contacts
  // the pull adds.
  ingest: (listId, limit) => post('/api/ingest', { list_id: listId, limit: limit ?? null }),
  reindex: () => post('/api/reindex'),
  progress: () => get('/api/progress'),
  enrollDryRun: () => post('/api/enroll/dry-run'),
  enrollLive: () => post('/api/enroll/live', { confirm: true }),
  trends: () => get('/api/trends'),
  demoProfiles: () => get('/api/demo/profiles'),
  connectors: () => get('/api/connectors'),
  // Wire a system up from Setup. The server never returns stored secrets, so
  // these only ever send values, never read them back.
  saveConnector: (id, values) => post(`/api/connectors/${id}`, { values }),
  testConnector: (id) => post(`/api/connectors/${id}/test`),
  disconnectConnector: (id) => post(`/api/connectors/${id}/disconnect`),
  home: () => get('/api/home'),
  // Chat-driven config editing: propose (read-only, returns a diff) -> apply -> revert.
  configScopes: () => get('/api/config/scopes'),
  configPropose: (body) => post('/api/config/propose', body),
  configApply: (body) => post('/api/config/apply', body),
  configRevert: (body) => post('/api/config/revert', body),
  refreshTrends: () => post('/api/trends/refresh'),
  generate: (batchId, variant) => post('/api/generate', { batch_id: batchId, variant }),
  generateStatus: (jobId) => get('/api/generate/status/' + jobId),
  generateCancel: (jobId) => post('/api/generate/cancel/' + jobId),
  submitBatch: (limit, variant, split) => post('/api/generate/batch', { limit, variant, split }),
  hubspotLists: (q, type) => {
    const p = new URLSearchParams()
    if (q) p.set('q', q)
    if (type) p.set('type', type)
    return get('/api/hubspot/lists?' + p.toString())
  },
  clayStatus: () => get('/api/clay/status'),
  clayConnectUrl: () => get('/api/clay/oauth/start'),
  sourceEnrich: (opts) => post('/api/source/enrich', opts),
  sourceConfirm: (jobId) => post('/api/source/confirm/' + jobId),
  sourceStatus: (jobId) => get('/api/source/status/' + jobId),
  sourceProgress: (listId) => get('/api/source/progress?list_id=' + encodeURIComponent(listId)),
  sourceProgressReset: (listId) => post('/api/source/progress/reset', { list_id: listId }),
  batchStatus: (jobId) => get('/api/generate/batch/status/' + jobId),
  batchList: () => get('/api/generate/batch/list'),
  cancelBatch: (jobId) => post('/api/generate/batch/cancel/' + jobId),
  scanReplies: (opts) => post('/api/replies/scan', opts || {}),
  repliesQueue: () => get('/api/replies/queue'),
  tagReplies: (replyIds) => post('/api/replies/tag', { reply_ids: replyIds, confirm: true }),
  draftFollowups: () => post('/api/replies/followup/draft'),
  followupDrafts: () => get('/api/replies/followup/drafts'),
  approveFollowup: (replyId, message) => post('/api/replies/followup/approve', { reply_id: replyId, message, confirm: true }),
  dismissReply: (replyId, reason) => post('/api/replies/dismiss', { reply_id: replyId, reason }),
  undismissReply: (replyId) => post('/api/replies/undismiss', { reply_id: replyId }),
  reclassifyReply: (replyId) => post('/api/replies/reclassify', { reply_id: replyId }),
  moveReply: (replyId, to) => post('/api/replies/followup/move', { reply_id: replyId, to }),
  repliesAgents: () => get('/api/replies/agents'),
  setReplyAgent: (replyId, agent) => post('/api/replies/agent', { reply_id: replyId, agent }),
  regenerateDraft: (replyId, agent, companyDomain) => post('/api/replies/followup/regenerate', { reply_id: replyId, agent, company_domain: companyDomain || undefined }),
  playbookStatus: (jobId) => get('/api/replies/playbook/status/' + jobId),
  systemStatus: () => get('/api/system/status'),
  // The play HTML is auth-gated, so fetch it with the bearer header and hand
  // back a blob URL the caller can window.open.
  playHtmlBlobUrl: async (slug) => {
    const res = await fetch(`/api/plays/${encodeURIComponent(slug)}/html`, { headers: authHeaders() })
    if (!res.ok) throw new Error(`${res.status} could not load the play preview`)
    return URL.createObjectURL(await res.blob())
  },
  signals: () => get('/api/signals'),
  // Signal definitions: what counts as a signal here. Preview writes nothing.
  signalDefs: () => get('/api/signals/definitions'),
  saveSignalDef: (body) => post('/api/signals/definitions', body),
  deleteSignalDef: (kind) => post(`/api/signals/definitions/${kind}/delete`),
  runSignalDef: (kind, body) => post(`/api/signals/definitions/${kind}/run`, body || {}),
  previewSignalRule: (body) => post('/api/signals/definitions/preview', body),
  signalDetail: (domain) => get('/api/signals/detail?domain=' + encodeURIComponent(domain)),
  refreshSignal: (domain) => post('/api/signals/refresh', { domain }),
  detectTech: (domain, force) => post('/api/signals/tech/detect', { domain, force: !!force }),
  techBackfill: (opts) => post('/api/signals/tech/backfill', opts || {}),
  techBackfillStatus: (jobId) => get('/api/signals/tech/status/' + jobId),
  detectHiring: (domain, force) => post('/api/signals/hiring/detect', { domain, force: !!force }),
  hiringBackfill: (opts) => post('/api/signals/hiring/backfill', opts || {}),
  hiringBackfillStatus: (jobId) => get('/api/signals/hiring/status/' + jobId),
  variants: () => get('/api/variants'),
  samples: (body) => post('/api/samples', body),
  // Campaigns — a defined set of accounts showing signal over a target window,
  // worked through a sequence whose every step declares the CTA it carries.
  campaigns: () => get('/api/campaigns'),
  campaign: (id) => get('/api/campaigns/' + id),
  createCampaign: (body) => post('/api/campaigns', body),
  updateCampaign: (id, body) => post('/api/campaigns/' + id, body),
  deleteCampaign: (id) => post(`/api/campaigns/${id}/delete`),
  upsertCampaignStep: (id, body) => post(`/api/campaigns/${id}/steps`, body),
  deleteCampaignStep: (id, body) => post(`/api/campaigns/${id}/steps/delete`, body),
  qualifyCampaign: (id, opts) => post(`/api/campaigns/${id}/qualify`, opts || {}),
  suggestStepCopy: (id, body) => post(`/api/campaigns/${id}/suggest`, body),
  // Discovery — actively scan in-scope accounts for signal. dry_run returns the
  // candidate list without scanning (the live run spends Prospeo credits).
  discoverAccounts: (id, body) => post(`/api/campaigns/${id}/discover`, body || {}),
  discoverStatus: (jobId) => get('/api/campaigns/discover/status/' + jobId),
  rescoreCampaign: (id, opts) => post(`/api/campaigns/${id}/rescore`, opts || {}),
  // Priority-ordered contacts to work. Omit campaignId for a cross-campaign list.
  // `state` filters SERVER-side; 'all' is the every-state sentinel and omitting it
  // leaves the server on its 'qualified' default. It cannot be sent as an empty
  // value — the server's parse_qs drops blanks, so `state=` reads as absent.
  // Filtering state in the browser (as this used to) only ever filtered a response
  // that already held nothing else, so "Enrolled" always came back empty.
  callList: (campaignId, limit, state) => {
    const p = new URLSearchParams()
    if (campaignId) p.set('campaign_id', campaignId)
    if (limit) p.set('limit', limit)
    if (state) p.set('state', state)
    return get('/api/campaigns/calllist?' + p.toString())
  },
  // Signal observations over time — what a campaign window actually catches.
  signalEvents: (days) => get('/api/signals/events?days=' + (days || 90)),
  // Audiences — WHICH accounts are in scope (a list, a CRM segment like
  // closed-lost, or everything), as opposed to which of them show signal.
  audienceVocab: () => get('/api/campaigns/audiences'),
  previewAudience: (audience) => post('/api/campaigns/audience/preview', { audience }),
  // Proposes a campaign configuration from a description / spec. Writes nothing —
  // the response is a patch for the builder form.
  campaignBrief: (body) => post('/api/campaigns/brief', body),
  // Drop a CSV/XLSX as an audience. `uploadPreview` writes nothing — it exists so
  // the column mapping can be corrected before contacts are created in the CRM.
  uploadPreview: (body) => post('/api/campaigns/audience/upload', body),
  uploadImport: (body) => post('/api/campaigns/audience/import', body),
  contactImports: () => get('/api/campaigns/imports'),
  // Evergreen campaigns waiting on a human before their next cycle opens.
  campaignReviews: () => get('/api/campaigns/reviews'),
  // A campaign's own written copy and replies — the same data as the app-wide
  // views, scoped to its members.
  campaignOutreach: (id) => get(`/api/campaigns/${id}/outreach`),
  campaignReplies: (id) => get(`/api/campaigns/${id}/replies`),
  campaignExcluded: (id) => get(`/api/campaigns/${id}/excluded`),
  // Working the call list. Two levels on purpose: `member` is scoped to one
  // campaign, `engagement` applies to the person everywhere and is enforced
  // by the enroll gate.
  updateMember: (body) => post('/api/calllist/member', body),
  updateEngagement: (body) => post('/api/calllist/engagement', body),
  // Content a CTA play carries — proof points and linked assets.
  references: () => get('/api/references'),
  saveReference: (body) => post('/api/references', body),
  attachContent: (body) => post('/api/references/attach', body),
  relaunchCampaign: (id, body) => post(`/api/campaigns/${id}/relaunch`, body || {}),
  // Clay enrichment — find the rest of the buyer group at a campaign's accounts.
  // dry_run returns the account list and a credit floor, and spends nothing.
  enrichCampaign: (id, body) => post(`/api/campaigns/${id}/enrich`, body || {}),
  enrichStatus: (jobId) => get('/api/campaigns/enrich/status/' + jobId),
  // Sending capacity (LinkedIn/day, email/month) + enrichment credit spend.
  capacity: (days) => get('/api/capacity?days=' + (days || 30)),
  // The daily hot-target report — top accounts across active campaigns.
  hotList: () => get('/api/campaigns/hotlist'),
  refreshHotList: () => post('/api/campaigns/hotlist/refresh'),
  // CRM field wiring. The CRM is the source of truth: push writes what we compute,
  // pull reads the CRM value back as authoritative.
  crmFields: () => get('/api/crm/fields'),
  updateCrmField: (body) => post('/api/crm/fields', body),
  crmSync: (body) => post('/api/crm/sync', body),
  // Console-campaign performance: does the score predict replies, which channels.
  campaignAnalytics: () => get('/api/analytics/campaigns'),
  // The end-to-end funnel: qualified -> enrolled -> contacted -> replied -> interested.
  funnel: () => get('/api/analytics/funnel'),
  // Packaging registry — which features are separately-sold agents/add-ons.
  tiers: () => get('/api/tiers'),
  buyerGroup: () => get('/api/buyer-group'),
  // Ad-hoc reporting. The model emits a constrained SPEC, never SQL — it is
  // validated server-side against the dataset registry before it runs.
  reportSchema: () => get('/api/reports/schema'),
  runReport: (spec) => post('/api/reports/run', { spec }),
  describeReport: (description) => post('/api/reports/describe', { description }),
  updateBuyerGroup: (body) => post('/api/buyer-group', body),
}
