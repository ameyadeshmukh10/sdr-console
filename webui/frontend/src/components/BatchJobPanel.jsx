import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'

const STATUS_COLOR = {
  processing: 'var(--accent)', done: 'var(--green)', cancelled: 'var(--amber)', error: 'var(--red)',
}

// Message Batches API (50% off, async). Submit N pending pipeline batches as one
// Anthropic batch; jobs are persisted server-side and survive reloads/restarts.
export default function BatchJobPanel({ pendingBatches, variant, split, splitValid = true, onChanged }) {
  const [jobs, setJobs] = useState(null)
  const [limit, setLimit] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const timer = useRef(null)

  const load = useCallback(async () => {
    try { setJobs((await api.batchList()).jobs); setError(null) }
    catch (e) { setError(e.message) }
  }, [])

  useEffect(() => { load() }, [load])
  // poll while any job is still processing
  useEffect(() => {
    const active = (jobs || []).some((j) => j.status === 'processing')
    clearInterval(timer.current)
    if (active) timer.current = setInterval(load, 15000)
    return () => clearInterval(timer.current)
  }, [jobs, load])

  async function submit() {
    setBusy(true); setError(null)
    try {
      const r = await api.submitBatch(Number(limit), variant, split || undefined)
      if (r.ok === false) setError(r.error || 'submit failed')
      await load(); onChanged?.()
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  async function cancel(jobId) {
    try { await api.cancelBatch(jobId); await load(); onChanged?.() }
    catch (e) { setError(e.message) }
  }

  return (
    <div className="panel" style={{ marginTop: 20 }}>
      <div className="row between">
        <span className="section-h" style={{ margin: 0 }}>Batch API · 50% off · async</span>
        <span className="muted" style={{ fontSize: 12 }}>{num(pendingBatches)} pending batches</span>
      </div>
      <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
        Submit pending batches to Anthropic's Message Batches API at half price. Results come back
        asynchronously (usually minutes, up to ~1h) — you can leave and check back.
      </p>

      <ErrorBanner error={error} />

      <div className="row" style={{ gap: 12, alignItems: 'flex-end', marginTop: 8 }}>
        <label className="field">Pending batches to submit
          <input type="number" min="1" max={Math.max(1, pendingBatches)} value={limit}
            onChange={(e) => setLimit(e.target.value)} style={{ width: 110 }} />
        </label>
        <button onClick={submit} disabled={busy || pendingBatches === 0 || (split && !splitValid)}>
          {busy ? <Spinner label="Submitting…" /> : `Submit ${Number(limit) || 0} → Batch API`}
        </button>
        {split && (
          <span className="muted" style={{ alignSelf: 'center', fontSize: 12 }}>
            split {Object.entries(split).map(([k, v]) => `${k} ${v}%`).join(' · ')}
          </span>
        )}
      </div>

      {jobs && jobs.length > 0 && (
        <div className="panel" style={{ padding: 0, marginTop: 16, maxHeight: 320, overflow: 'auto' }}>
          <table>
            <thead><tr><th>Job</th><th>Status</th><th>Requests</th><th>Done</th><th>Errored</th><th>Result</th><th></th></tr></thead>
            <tbody>
              {jobs.map((j) => {
                const c = j.counts || {}
                return (
                  <tr key={j.job_id}>
                    <td>
                      <div className="mono">{j.job_id}</div>
                      <div className="muted" style={{ fontSize: 11 }}>{j.write_only}/{j.request_count} cached</div>
                    </td>
                    <td>
                      <span className="badge" style={{ color: STATUS_COLOR[j.status] || 'var(--muted)', borderColor: STATUS_COLOR[j.status] || 'var(--border)' }}>
                        {j.status === 'processing' ? <span className="row" style={{ gap: 5 }}><span className="spinner" />processing</span> : j.status}
                      </span>
                    </td>
                    <td>{num(j.request_count)}</td>
                    <td>{num(c.succeeded || 0)}</td>
                    <td>{num((c.errored || 0) + (c.expired || 0))}</td>
                    <td className="muted">
                      {j.status === 'done' ? `${num(j.summary?.linted || 0)} linted · ${num(j.summary?.failed || 0)} failed` : '—'}
                    </td>
                    <td>
                      {j.status === 'processing' &&
                        <button className="ghost sm" onClick={() => cancel(j.job_id)}>Cancel</button>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
