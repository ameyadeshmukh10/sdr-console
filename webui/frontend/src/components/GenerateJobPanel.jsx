import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'

const STATE_COLOR = {
  queued: 'var(--muted)', researching: 'var(--accent)', linted: 'var(--green)',
  failed: 'var(--red)', error: 'var(--red)', cancelled: 'var(--amber)',
}
const STATE_LABEL = {
  queued: 'queued', researching: 'researching + writing', linted: 'done',
  failed: 'failed', error: 'error', cancelled: 'cancelled',
}

// Live view of a copy-generation job. Polls /api/generate/status until done.
export default function GenerateJobPanel({ jobId, onDone }) {
  const [job, setJob] = useState(null)
  const [error, setError] = useState(null)
  const timer = useRef(null)
  const firedDone = useRef(false)

  useEffect(() => {
    firedDone.current = false
    async function poll() {
      try {
        const j = await api.generateStatus(jobId)
        setJob(j); setError(null)
        if (j.status !== 'running') {
          clearInterval(timer.current)
          if (!firedDone.current) { firedDone.current = true; onDone?.() }
        }
      } catch (e) { setError(e.message) }
    }
    poll()
    timer.current = setInterval(poll, 2000)
    return () => clearInterval(timer.current)
  }, [jobId])

  if (error) return <ErrorBanner error={error} />
  if (!job) return <Spinner label="Starting job…" />

  const contacts = Object.values(job.contacts || {})
  const order = { researching: 0, queued: 1, linted: 2, failed: 3, error: 3, cancelled: 4 }
  contacts.sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9))
  const running = job.status === 'running'

  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <div className="row between">
        <span className="section-h" style={{ margin: 0 }}>
          Generation job <span className="mono">{job.job_id}</span> · batch #{job.batch_id}
        </span>
        <span className="row" style={{ gap: 10 }}>
          {running
            ? <span className="row" style={{ gap: 6 }}><span className="spinner" />running</span>
            : <span className="badge" style={{ color: job.status === 'done' ? 'var(--green)' : 'var(--amber)' }}>{job.status}</span>}
          {running && <button className="ghost" onClick={() => api.generateCancel(jobId)}>Cancel</button>}
        </span>
      </div>

      <div className="row" style={{ gap: 18, margin: '10px 0' }}>
        <span><b>{num(job.summary.linted)}</b> linted</span>
        <span className="muted">{num(job.summary.failed)} failed · {num(contacts.length)} contacts</span>
      </div>

      <div className="panel" style={{ padding: 0, maxHeight: 300, overflow: 'auto', marginBottom: 12 }}>
        <table>
          <thead><tr><th>Contact</th><th>Company</th><th>State</th><th>Searches</th><th>Issues</th></tr></thead>
          <tbody>
            {contacts.map((c) => (
              <tr key={c.contact_id}>
                <td>{c.name || c.contact_id}</td>
                <td className="muted">{c.company || '—'}</td>
                <td><span className="badge" style={{ color: STATE_COLOR[c.state], borderColor: STATE_COLOR[c.state] }}>
                  {STATE_LABEL[c.state] || c.state}</span></td>
                <td>{c.web_searches ?? '—'}</td>
                <td className="muted" style={{ maxWidth: 240, fontSize: 12 }}>
                  {(c.issues && c.issues.length) ? c.issues.join('; ').slice(0, 140) : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {job.log && job.log.length > 0 && (
        <div className="log">{job.log.join('\n')}</div>
      )}
      {job.error && <div className="banner error" style={{ marginTop: 10 }}>{job.error}</div>}
    </div>
  )
}
