import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, Badge, num } from '../components/ui.jsx'
import EnrollPanel from '../components/EnrollPanel.jsx'

// Pipeline — live batch progress (poll the DB while /sdr-batches runs in Claude
// Code) + the enrollment gate. The web server can't generate copy itself, so
// progress here reflects work the sub-agents are doing, surfaced in real time.
const POLL_MS = 2500

export default function PipelinePage() {
  const [prog, setProg] = useState(null)
  const [error, setError] = useState(null)
  const [auto, setAuto] = useState(true)
  const [lastTick, setLastTick] = useState(null)
  const timer = useRef(null)

  const poll = useCallback(async () => {
    try {
      const p = await api.progress()
      setProg(p); setError(null); setLastTick(new Date())
    } catch (e) { setError(e.message) }
  }, [])

  useEffect(() => { poll() }, [poll])
  useEffect(() => {
    if (!auto) { clearInterval(timer.current); return }
    timer.current = setInterval(poll, POLL_MS)
    return () => clearInterval(timer.current)
  }, [auto, poll])

  const bstat = prog?.batches_by_status || {}
  const done = bstat.done || 0
  const total = prog?.total_batches || 0
  const inProgress = bstat.in_progress || 0
  const pending = bstat.pending || 0
  const pctDone = total ? Math.round((100 * done) / total) : 0
  const cstat = prog?.contacts_by_status || {}

  return (
    <div>
      <div className="row between">
        <div>
          <h1 className="page-title">Pipeline</h1>
          <p className="page-sub">Live batch generation progress, then enroll the generated copy into Bison.</p>
        </div>
        <div className="row" style={{ gap: 12 }}>
          <label className="row" style={{ gap: 6, fontSize: 13, color: 'var(--muted)' }}>
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} style={{ width: 'auto' }} />
            Auto-refresh
          </label>
          <button className="ghost" onClick={poll}>↻ Now</button>
        </div>
      </div>

      <ErrorBanner error={error} />

      {!prog ? <Spinner label="Loading…" /> : (
        <>
          <div className="panel" style={{ marginBottom: 20 }}>
            <div className="row between" style={{ marginBottom: 10 }}>
              <span className="section-h" style={{ margin: 0 }}>Batch progress</span>
              <span className="muted" style={{ fontSize: 12 }}>
                {auto ? <span className="row" style={{ gap: 6 }}><span className="spinner" />live</span> : 'paused'}
                {lastTick && ` · updated ${lastTick.toLocaleTimeString()}`}
              </span>
            </div>
            <div style={{ background: 'var(--panel-2)', borderRadius: 8, height: 22, overflow: 'hidden', border: '1px solid var(--border)' }}>
              <div style={{ width: `${pctDone}%`, height: '100%', background: 'var(--green)', transition: 'width .4s' }} />
            </div>
            <div className="row" style={{ gap: 18, marginTop: 12 }}>
              <span><b>{num(done)}</b> / {num(total)} batches done ({pctDone}%)</span>
              <span className="muted">{num(inProgress)} in progress · {num(pending)} pending</span>
            </div>
          </div>

          <div className="grid stat-grid" style={{ marginBottom: 22 }}>
            <Stat label="Pending" value={num(cstat.pending || 0)} sub="awaiting generation" />
            <Stat label="Generated" value={num(cstat.generated || 0)} sub="ready to enroll" />
            <Stat label="Enrolled" value={num(cstat.enrolled || 0)} />
            <Stat label="Skipped / failed" value={num((cstat.skipped || 0) + (cstat.failed || 0))} />
          </div>

          {prog.active_batches.length > 0 ? (
            <>
              <h2 className="section-h">Active batches</h2>
              <div className="panel" style={{ padding: 0, marginBottom: 24 }}>
                <table>
                  <thead><tr><th>Batch</th><th>Status</th><th>Size</th><th>Generated</th><th>Failed</th><th>Pending</th></tr></thead>
                  <tbody>
                    {prog.active_batches.map((b) => (
                      <tr key={b.batch_id}>
                        <td className="mono">#{b.batch_id}</td>
                        <td><Badge kind="status" value={b.status === 'in_progress' ? 'generated' : 'pending'} /></td>
                        <td>{b.size}</td>
                        <td>{b.counts.generated || 0}</td>
                        <td>{b.counts.failed || 0}</td>
                        <td>{b.counts.pending || 0}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <div className="banner info" style={{ marginBottom: 24 }}>
              No active batches — all {num(total)} batches are done. Generate more by ingesting a list and running <span className="mono">/sdr-batches</span> in Claude Code; progress will appear here live.
            </div>
          )}

          <EnrollPanel generatedReady={prog.generated_ready} onChanged={poll} />
        </>
      )}
    </div>
  )
}
