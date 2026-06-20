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
  outreach: (params) => get('/api/outreach?' + new URLSearchParams(params).toString()),
  outreachDetail: (id) => get('/api/outreach/' + encodeURIComponent(id)),
  ingest: (listId) => post('/api/ingest', { list_id: listId }),
  reindex: () => post('/api/reindex'),
  progress: () => get('/api/progress'),
  enrollDryRun: () => post('/api/enroll/dry-run'),
  enrollLive: () => post('/api/enroll/live', { confirm: true }),
  trends: () => get('/api/trends'),
  refreshTrends: () => post('/api/trends/refresh'),
  generate: (batchId) => post('/api/generate', { batch_id: batchId }),
  generateStatus: (jobId) => get('/api/generate/status/' + jobId),
  generateCancel: (jobId) => post('/api/generate/cancel/' + jobId),
  submitBatch: (limit) => post('/api/generate/batch', { limit }),
  batchStatus: (jobId) => get('/api/generate/batch/status/' + jobId),
  batchList: () => get('/api/generate/batch/list'),
  cancelBatch: (jobId) => post('/api/generate/batch/cancel/' + jobId),
  scanReplies: (opts) => post('/api/replies/scan', opts || {}),
  repliesQueue: () => get('/api/replies/queue'),
  tagReplies: (replyIds) => post('/api/replies/tag', { reply_ids: replyIds, confirm: true }),
  signals: () => get('/api/signals'),
  refreshSignal: (domain) => post('/api/signals/refresh', { domain }),
}
