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

function authHeaders(extra) {
  const h = { ...(extra || {}) }
  const t = getToken()
  if (t) h.Authorization = 'Bearer ' + t
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
    throw new Error(`${res.status} ${detail}`.trim())
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
  if (!res.ok) throw new Error(data.error || `${res.status}`)
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
  rollup: () => get('/api/rollup'),
  analytics: () => get('/api/analytics'),
  refreshAnalytics: () => post('/api/analytics/refresh'),
  linkedinAnalytics: () => get('/api/analytics/linkedin'),
  aisdrAnalytics: () => get('/api/analytics/aisdr'),
  aisdrSyncStatus: () => get('/api/hubspot/aisdr/status'),
  aisdrSync: (opts) => post('/api/hubspot/aisdr/sync', opts || {}),
  outreach: (params) => get('/api/outreach?' + new URLSearchParams(params).toString()),
  outreachDetail: (id) => get('/api/outreach/' + encodeURIComponent(id)),
  ingest: (listId) => post('/api/ingest', { list_id: listId }),
  reindex: () => post('/api/reindex'),
  progress: () => get('/api/progress'),
  enrollDryRun: () => post('/api/enroll/dry-run'),
  enrollLive: () => post('/api/enroll/live', { confirm: true }),
  trends: () => get('/api/trends'),
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
  regenerateDraft: (replyId, agent) => post('/api/replies/followup/regenerate', { reply_id: replyId, agent }),
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
  refreshSignal: (domain) => post('/api/signals/refresh', { domain }),
  variants: () => get('/api/variants'),
  samples: (body) => post('/api/samples', body),
}
