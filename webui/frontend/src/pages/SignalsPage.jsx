import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from '../components/ui.jsx'

// Signal cache — per-company research reused for 90 days so a company is searched
// once instead of once per contact / per re-run. Force-refresh re-searches one.
// Tech = the technographic scan (website + DNS fingerprinting) stored alongside:
// per-row Detect re-scans one company, "Detect missing" backfills the rest.
const NO_TECH = 'No signals detected'

export default function SignalsPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(null)
  const [detecting, setDetecting] = useState(null)
  const [bulkJob, setBulkJob] = useState(null)

  function load() {
    api.signals().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])

  async function refresh(domain) {
    setRefreshing(domain); setError(null)
    try {
      const d = await api.refreshSignal(domain)
      if (d.ok === false) setError(d.error || 'refresh failed')
      else setData(d)
    } catch (e) { setError(e.message) }
    finally { setRefreshing(null) }
  }

  async function detect(domain) {
    setDetecting(domain); setError(null)
    try {
      const d = await api.detectTech(domain, true)
      if (d.ok === false) setError(d.error || 'tech detect failed')
      else setData(d)
    } catch (e) { setError(e.message) }
    finally { setDetecting(null) }
  }

  async function startBulk() {
    setError(null)
    try {
      const d = await api.techBackfill({})
      setBulkJob({ job_id: d.job_id, status: 'running', total: d.total, done: 0 })
    } catch (e) { setError(e.message) }
  }

  // Poll the bulk job while it runs; reload the table when it lands.
  useEffect(() => {
    if (!bulkJob || bulkJob.status !== 'running') return
    const t = setInterval(async () => {
      try {
        const j = await api.techBackfillStatus(bulkJob.job_id)
        setBulkJob(j)
        if (j.status !== 'running') load()
      } catch (e) { setBulkJob(null); setError(e.message) }
    }, 2500)
    return () => clearInterval(t)
  }, [bulkJob?.job_id, bulkJob?.status])

  const signals = data?.signals || []
  const fresh = signals.filter((s) => s.fresh).length
  const recent = signals.filter((s) => s.has_recent).length
  const scanned = signals.filter((s) => s.tech_age_days != null).length
  const withTech = signals.filter((s) => s.tech_signals && s.tech_signals !== NO_TECH).length
  const missing = signals.filter((s) => s.tech_age_days == null).length
  const techOff = data ? data.tech_available === false : false
  const bulkRunning = bulkJob?.status === 'running'

  return (
    <div>
      <h1 className="page-title">Signals</h1>
      <p className="page-sub">Per-company research cache. Fresh entries (&lt;90 days) are reused, so the AI SDR skips the web search. Tech = detected stack from a website + DNS scan.</p>

      <ErrorBanner error={error} />

      <div className="grid stat-grid" style={{ marginBottom: 20 }}>
        <Stat label="Cached accounts" value={num(data?.count || 0)} />
        <Stat label="Fresh (<90d)" value={num(fresh)} sub="reused, no re-search" />
        <Stat label="Real signal" value={num(recent)} tone="good" />
        <Stat label="Fallback (no signal)" value={num(signals.length - recent)} sub="product/GTM anchor" tone="warn" />
        <Stat label="Tech scanned" value={techOff ? '—' : num(scanned)}
          sub={techOff ? (data?.tech_reason || 'detection unavailable') : `${num(withTech)} with detections`}
          tone={techOff ? 'warn' : undefined} />
      </div>

      {!techOff && data && (missing > 0 || bulkJob) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10 }}>
          {missing > 0 && !bulkRunning && (
            <button className="ghost sm" onClick={startBulk}>Detect missing ({num(missing)})</button>
          )}
          {bulkJob && (
            <span className="muted" style={{ fontSize: 13 }}>
              {bulkRunning
                ? <>Detecting tech… {bulkJob.done}/{bulkJob.total}{bulkJob.current ? ` (${bulkJob.current})` : ''}{bulkJob.errors ? ` · ${bulkJob.errors} errors` : ''}</>
                : bulkJob.status === 'done'
                  ? <>Tech backfill done: {bulkJob.detected} detected, {bulkJob.skipped} skipped{bulkJob.errors ? `, ${bulkJob.errors} errors` : ''}</>
                  : <>Tech backfill failed: {bulkJob.error || 'unknown error'}</>}
            </span>
          )}
        </div>
      )}

      {!data ? <Spinner label="Loading…" /> : signals.length === 0 ? (
        <div className="empty">No cached signals yet. They populate as you generate batches.</div>
      ) : (
        <div className="panel" style={{ padding: 0 }}>
          <table className="dense">
            <thead><tr>
              <th style={{ width: '13%' }}>Domain</th>
              <th style={{ width: '12%' }}>Company</th>
              <th style={{ width: '7%' }}>Type</th>
              <th style={{ width: '32%' }}>Signal</th>
              <th style={{ width: '24%' }}>Tech</th>
              <th style={{ width: '5%' }}>Age</th>
              <th></th>
            </tr></thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.domain}>
                  <td className="mono" style={{ whiteSpace: 'nowrap' }}>{s.domain}</td>
                  <td>
                    {s.company_name
                      ? <span title={s.company_name} style={{ display: 'inline-block', maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>{s.company_name}</span>
                      : <span className="muted">—</span>}
                  </td>
                  <td>
                    {s.has_recent
                      ? <span className="badge" style={{ color: 'var(--green)', borderColor: 'var(--green)' }}>recent</span>
                      : <span className="badge" style={{ color: 'var(--amber)', borderColor: 'var(--amber)' }}>fallback</span>}
                  </td>
                  <td className="muted" style={{ maxWidth: 340 }}>
                    <span className="clamp2" title={s.signal}>{s.signal}</span>
                  </td>
                  <td style={{ maxWidth: 260 }}>
                    {s.tech_signals && s.tech_signals !== NO_TECH ? (
                      <span className="muted" title={`${s.tech_signals}${s.tech_age_days != null ? ` (scanned ${s.tech_age_days}d ago)` : ''}`}
                        style={{ display: 'inline-block', maxWidth: 250, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>
                        {s.tech_signals}
                      </span>
                    ) : s.tech_signals === NO_TECH ? (
                      <span className="muted" title={s.tech_age_days != null ? `scanned ${s.tech_age_days}d ago` : undefined}>none detected</span>
                    ) : s.tech_error ? (
                      <span className="badge" style={{ color: 'var(--red)', borderColor: 'var(--red)' }} title={s.tech_error}>scan failed</span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    <span style={{ color: s.fresh ? 'var(--muted)' : 'var(--red)' }}>
                      {s.age_days == null ? '—' : `${s.age_days}d`}
                    </span>
                  </td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <button className="ghost sm" disabled={refreshing === s.domain} onClick={() => refresh(s.domain)}>
                      {refreshing === s.domain ? <Spinner /> : '↻ Refresh'}
                    </button>{' '}
                    <button className="ghost sm" disabled={techOff || detecting === s.domain} onClick={() => detect(s.domain)}
                      title={techOff ? (data?.tech_reason || 'detection unavailable') : 'Re-scan this company’s website + DNS'}>
                      {detecting === s.domain ? <Spinner /> : '⌁ Detect'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
