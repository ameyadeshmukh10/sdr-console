import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from '../components/ui.jsx'

// Pillar 1 — Use: feed a HubSpot list into the pipeline (pull + init), then show
// the resulting pipeline state and any pending batches awaiting generation.
export default function UsePage() {
  const [status, setStatus] = useState(null)
  const [pending, setPending] = useState([])
  const [listId, setListId] = useState('2198')
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [showLog, setShowLog] = useState(false)

  async function refresh() {
    try {
      const [s, b] = await Promise.all([api.status(), api.batches('pending')])
      setStatus(s)
      setPending(b.batches)
    } catch (e) { setError(e.message) }
  }

  useEffect(() => { refresh() }, [])

  async function runIngest() {
    setRunning(true); setError(null); setResult(null)
    try {
      const r = await api.ingest(listId.trim())
      setResult(r)
      if (!r.ok) setError(`Ingest failed at ${r.stage || 'unknown'} stage`)
      await refresh()
    } catch (e) { setError(e.message) }
    finally { setRunning(false) }
  }

  const log = result && [result.pull, result.init]
    .filter(Boolean)
    .map((s) => (s.stdout || '') + (s.stderr ? '\n[stderr]\n' + s.stderr : ''))
    .join('\n\n')

  return (
    <div>
      <h1 className="page-title">Use — feed the AI SDR</h1>
      <p className="page-sub">Pull a HubSpot list into the pipeline and batch it for the SDR sub-agents.</p>

      <ErrorBanner error={error} />

      <div className="panel" style={{ marginBottom: 22 }}>
        <div className="toolbar" style={{ marginBottom: 0 }}>
          <label className="field">
            HubSpot list ID
            <input value={listId} onChange={(e) => setListId(e.target.value)} style={{ width: 160 }} />
          </label>
          <button onClick={runIngest} disabled={running || !listId.trim()}>
            {running ? <Spinner label="Pulling + batching…" /> : 'Run pull + init'}
          </button>
          <span className="muted" style={{ alignSelf: 'center' }}>
            Runs <span className="mono">hubspot_pull.py</span> then <span className="mono">sdr_batches.py init</span>. Copy is generated separately via <span className="mono">/sdr-batches</span>.
          </span>
        </div>

        {result && result.ok && (
          <div className="banner info" style={{ marginTop: 16, marginBottom: 0 }}>
            Added <b>{num(result.new_contacts)}</b> new contacts and <b>{num(result.new_batches)}</b> new batches.
            {result.new_contacts === 0 && ' (List already ingested — init is idempotent.)'}
          </div>
        )}
        {log && (
          <>
            <button className="ghost" style={{ marginTop: 12 }} onClick={() => setShowLog((v) => !v)}>
              {showLog ? 'Hide' : 'Show'} run log
            </button>
            {showLog && <div className="log">{log}</div>}
          </>
        )}
      </div>

      {status && (
        <div className="grid stat-grid" style={{ marginBottom: 22 }}>
          <Stat label="Total contacts" value={num(status.total_contacts)} />
          <Stat label="Enrolled" value={num(status.contacts_by_status.enrolled || 0)} />
          <Stat label="Pending" value={num(status.contacts_by_status.pending || 0)} />
          <Stat label="Batches done" value={num(status.batches_by_status.done || 0)}
            sub={`${status.batches_by_status.pending || 0} pending`} />
        </div>
      )}

      <h2 className="section-h">Pending batches</h2>
      {pending.length === 0 ? (
        <div className="empty">No pending batches — everything generated has been processed.<br />
          Ingest a list above, then run <span className="mono">/sdr-batches</span> in Claude Code to generate copy.</div>
      ) : (
        <div className="panel" style={{ padding: 0 }}>
          <table>
            <thead><tr><th>Batch</th><th>Size</th><th>Status</th></tr></thead>
            <tbody>
              {pending.map((b) => (
                <tr key={b.batch_id}>
                  <td className="mono">#{b.batch_id}</td>
                  <td>{b.size}</td>
                  <td><span className="badge status-pending">pending</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
