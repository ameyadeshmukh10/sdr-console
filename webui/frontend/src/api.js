// Thin fetch wrappers around the Python backend. All paths are relative, so
// the Vite dev proxy (dev) and the Python static server (prod) both work.

async function get(path) {
  const res = await fetch(path)
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
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.error || `${res.status}`)
  return data
}

export const api = {
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
  hubspotActivityStatus: () => get('/api/hubspot/activity/status'),
  syncHubspotActivity: (opts) => post('/api/hubspot/activity/sync', opts || {}),
  signals: () => get('/api/signals'),
  refreshSignal: (domain) => post('/api/signals/refresh', { domain }),
  variants: () => get('/api/variants'),
  samples: (body) => post('/api/samples', body),
}
