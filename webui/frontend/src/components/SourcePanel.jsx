import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'

// Clay buying-group enrichment for a selected HubSpot company list. Connects Clay
// (OAuth) if needed, then runs the backend enrich job: end-to-end (commit
// immediately) or review (pause on the candidates, then Approve & create).
const POLL_MS = 2500

// Mirrors clay_enrich.py's JOB_TITLE_KEYWORDS — the default ICP target titles.
const DEFAULT_TITLES =
  'CRO, Chief Revenue Officer, VP of Sales, Head of Sales, Director of Sales, ' +
  'SDR Manager, BDR Manager, Sales Development Manager, Director of Revenue Operations, ' +
  'Revenue Operations, RevOps, Sales Operations, Sales Ops'

export default function SourcePanel({ list, onChanged }) {
  const [clay, setClay] = useState(null)        // connection status string
  const [mode, setMode] = useState('end-to-end')
  const [cap, setCap] = useState(25)
  // Advanced enrichment knobs (surfaced from clay_enrich.py).
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [perCompanyCap, setPerCompanyCap] = useState('')   // '' = no limit
  const [titles, setTitles] = useState(DEFAULT_TITLES)
  const [locations, setLocations] = useState('United States')
  const [concurrency, setConcurrency] = useState(8)
  const [jobId, setJobId] = useState(null)
  const [job, setJob] = useState(null)
  const [pollKey, setPollKey] = useState(0)     // bump to (re)start polling
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [progress, setProgress] = useState(null)   // { enriched } for this list
  const timer = useRef(null)

  const loadClay = useCallback(async () => {
    try { setClay((await api.clayStatus()).status) } catch (e) { setError(e.message) }
  }, [])
  useEffect(() => { loadClay() }, [loadClay])

  // Cursor progress for the selected list (how many companies already enriched).
  const loadProgress = useCallback(async () => {
    try { setProgress(await api.sourceProgress(list.list_id)) } catch { /* non-fatal */ }
  }, [list.list_id])
  useEffect(() => { loadProgress() }, [loadProgress])

  // poll the running job
  useEffect(() => {
    clearInterval(timer.current)
    if (!jobId) return
    const tick = async () => {
      try {
        const j = await api.sourceStatus(jobId)
        setJob(j)
        if (['done', 'error', 'awaiting_review'].includes(j.status)) {
          clearInterval(timer.current)
          if (j.status === 'done') { onChanged?.(); loadProgress() }
        }
      } catch (e) { setError(e.message) }
    }
    tick()
    timer.current = setInterval(tick, POLL_MS)
    return () => clearInterval(timer.current)
  }, [jobId, pollKey, onChanged, loadProgress])

  async function resetProgress() {
    if (!window.confirm('Start over? This clears the enrichment cursor so the next run begins from the top of the list.')) return
    try { await api.sourceProgressReset(list.list_id); await loadProgress() }
    catch (e) { setError(e.message) }
  }

  async function connectClay() {
    setError(null)
    try {
      const r = await api.clayConnectUrl()
      if (!r.ok) { setError(r.error || 'could not start Clay auth'); return }
      window.open(r.authorize_url, '_blank', 'noopener')
      // poll for the connection to complete
      const iv = setInterval(async () => {
        const s = (await api.clayStatus()).status
        setClay(s)
        if (s === 'connected') clearInterval(iv)
      }, 2500)
      setTimeout(() => clearInterval(iv), 180000)
    } catch (e) { setError(e.message) }
  }

  async function enrich() {
    setBusy(true); setError(null); setJob(null)
    try {
      const r = await api.sourceEnrich({
        list_id: list.list_id,
        list_name: `AI SDR Sourced — ${list.name}`,
        cap: Number(cap) || 25, mode,
        per_company_cap: Number(perCompanyCap) || 0,
        concurrency: Number(concurrency) || 8,
        titles: titles.trim() === DEFAULT_TITLES ? '' : titles.trim(),
        locations: locations.trim() === 'United States' ? '' : locations.trim(),
      })
      if (r.ok === false) setError(r.error || 'enrich failed to start')
      else setJobId(r.job_id)
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  async function approve() {
    setBusy(true); setError(null)
    try {
      const r = await api.sourceConfirm(jobId)
      if (r.ok === false) setError(r.error || 'confirm failed')
      else { setJob((j) => ({ ...j, status: 'running' })); setPollKey((k) => k + 1) }
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  const connected = clay === 'connected'
  const running = job && ['running'].includes(job.status)

  return (
    <div className="panel" style={{ marginBottom: 22 }}>
      <div className="row between" style={{ marginBottom: 8 }}>
        <span className="section-h" style={{ margin: 0 }}>Enrich buying group via Clay</span>
        <span className="muted" style={{ fontSize: 12 }}>
          Company list <b>{list.name}</b> <span className="mono">#{list.list_id}</span>
        </span>
      </div>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Finds GTM-leadership contacts at each company via Clay, dedups against HubSpot,
        creates the net-new contacts + a static list, and feeds them into the pipeline.
      </p>

      <ErrorBanner error={error} />

      {/* Clay connection */}
      {!connected ? (
        <div className="banner info" style={{ marginBottom: 12 }}>
          Clay is <b>{clay || '…'}</b>.{' '}
          <button onClick={connectClay} style={{ marginLeft: 8 }}>Connect Clay</button>
        </div>
      ) : (
        <div className="muted" style={{ fontSize: 12, marginBottom: 12 }}>Clay connected ✓</div>
      )}

      {/* Auto-advance cursor: how far through the list we are. */}
      {progress && (
        <div className="banner info" style={{ marginBottom: 12, fontSize: 13 }}>
          {progress.enriched > 0 ? (
            <>
              <b>{num(progress.enriched)}</b>
              {list.size ? <> of {num(list.size)}</> : null} companies enriched so far.
              {' '}The next run takes the next {Number(cap) || 25}.
              <button className="linklike" style={{ marginLeft: 10, fontSize: 12 }}
                onClick={resetProgress}>Start over</button>
            </>
          ) : (
            <>No companies enriched yet — the next run starts from the top
              {list.size ? <> of {num(list.size)}</> : null}.</>
          )}
        </div>
      )}

      <div className="toolbar" style={{ marginBottom: 0 }}>
        <label className="field">Mode
          <select value={mode} onChange={(e) => setMode(e.target.value)} style={{ minWidth: 180 }}>
            <option value="end-to-end">Run end-to-end</option>
            <option value="review">Pause for review</option>
          </select>
        </label>
        <label className="field">Max companies
          <input type="number" min="1" max="200" value={cap}
            onChange={(e) => setCap(e.target.value)} style={{ width: 100 }} />
        </label>
        <button onClick={enrich} disabled={!connected || busy || running}>
          {busy || running ? <Spinner label="Enriching…" /> : 'Enrich buying group'}
        </button>
      </div>

      <button className="linklike" onClick={() => setShowAdvanced((v) => !v)}
        style={{ marginTop: 10, fontSize: 12 }}>
        {showAdvanced ? '▾ Advanced settings' : '▸ Advanced settings'}
      </button>

      {showAdvanced && (
        <div className="panel" style={{ marginTop: 8, background: 'transparent' }}>
          <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
            Controls how Clay finds the buying group at each company. Each company yields a
            variable number of contacts (often 0), so total contacts ≠ max companies.
          </p>
          <div className="toolbar" style={{ marginBottom: 10 }}>
            <label className="field">Contacts per company
              <input type="number" min="0" max="25" value={perCompanyCap}
                placeholder="no limit"
                onChange={(e) => setPerCompanyCap(e.target.value)} style={{ width: 120 }} />
            </label>
            <label className="field">Concurrency
              <input type="number" min="1" max="16" value={concurrency}
                onChange={(e) => setConcurrency(e.target.value)} style={{ width: 100 }} />
            </label>
          </div>
          <label className="field" style={{ display: 'block', marginBottom: 10 }}>Locations (comma-separated)
            <input type="text" value={locations}
              onChange={(e) => setLocations(e.target.value)}
              style={{ width: '100%', boxSizing: 'border-box', marginTop: 4 }} />
          </label>
          <label className="field" style={{ display: 'block' }}>Target titles (comma-separated)
            <textarea value={titles} onChange={(e) => setTitles(e.target.value)}
              rows={3} style={{ width: '100%', boxSizing: 'border-box', fontSize: 12, marginTop: 4 }} />
          </label>
          <button className="linklike" style={{ fontSize: 12 }}
            onClick={() => { setTitles(DEFAULT_TITLES); setLocations('United States'); setPerCompanyCap(''); setConcurrency(8) }}>
            Reset to defaults
          </button>
        </div>
      )}

      {job && <JobView job={job} onApprove={approve} busy={busy} />}
    </div>
  )
}

function JobView({ job, onApprove, busy }) {
  if (job.status === 'error') {
    return <div className="banner error" style={{ marginTop: 14 }}>{job.error}</div>
  }
  if (job.status === 'running') {
    const p = job.progress || {}
    const total = p.total || 0
    const firing = p.phase === 'firing'
    const value = firing ? (p.fired || 0) : (p.completed || 0)
    const pct = total ? Math.round((100 * value) / total) : 0
    const label = !total ? 'Starting Clay enrichment…'
      : firing ? `Preparing ${value}/${total} companies…`
        : `Enriching ${value}/${total} companies…`
    return (
      <div style={{ marginTop: 14 }}>
        <Spinner label={label} />
        {total > 0 && (
          <div style={{ marginTop: 10 }}>
            <div style={{ height: 8, background: 'var(--border)', borderRadius: 4, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${pct}%`, background: 'var(--accent, #0a7)', transition: 'width .4s ease' }} />
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              {firing
                ? `firing ${value}/${total} enrichment tasks at Clay — they run in parallel`
                : `${value} of ${total} companies · ${p.contacts || 0} contact${p.contacts === 1 ? '' : 's'} found so far · each can take up to ~5 min on Clay's side`}
            </div>
          </div>
        )}
      </div>
    )
  }
  if (job.status === 'awaiting_review') {
    const cands = job.candidates || []
    return (
      <div style={{ marginTop: 14 }}>
        <div className="row between" style={{ marginBottom: 8 }}>
          <b>{num(cands.length)} candidate contacts</b>
          <button onClick={onApprove} disabled={busy || cands.length === 0}>
            {busy ? <Spinner label="Creating…" /> : 'Approve & create in HubSpot'}
          </button>
        </div>
        <div className="panel" style={{ padding: 0, maxHeight: 320, overflow: 'auto' }}>
          <table>
            <thead><tr><th>Name</th><th>Title</th><th>Email</th><th>Company</th></tr></thead>
            <tbody>
              {cands.map((c, i) => (
                <tr key={c.email || i}>
                  <td>{[c.first_name, c.last_name].filter(Boolean).join(' ') || '—'}</td>
                  <td className="muted">{c.title || '—'}</td>
                  <td className="mono">{c.email}</td>
                  <td className="muted">{c.company || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    )
  }
  if (job.status === 'done') {
    const s = job.stats
    return (
      <div className="banner info" style={{ marginTop: 14 }}>
        {s ? (
          <>Created <b>{num(s.created)}</b> net-new contacts
            {' '}({num(s.already_in_hubspot)} already in HubSpot){s.hubspot_list_id ? <> · list <span className="mono">#{s.hubspot_list_id}</span></> : null}.
            {' '}Open the <b>Pipeline</b> tab to generate copy.</>
        ) : (job.source?.note || 'Done.')}
      </div>
    )
  }
  return null
}
