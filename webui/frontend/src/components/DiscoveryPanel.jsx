import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import Addon from './Addon.jsx'

// "Find accounts" — actively scan in-scope accounts for signal.
//
// Qualification can only read signals something else already observed, so a new
// campaign starts empty and there is no obvious way forward. Discovery is the
// missing verb: it scans accounts in the campaign's persona/motion scope that have
// never been scanned, then re-qualifies. "0 accounts match, 340 never scanned" is a
// far more actionable state than "0 accounts match".
//
// The hiring detector spends one Prospeo credit per domain, so the cost is stated on
// the button, the default limit is deliberately small, and Preview scans nothing.

const PRESETS = [10, 25, 50, 100]

export default function DiscoveryPanel({ campaignId, discovery, onDone }) {
  const [limit, setLimit] = useState(25)
  const [job, setJob] = useState(null)
  const [preview, setPreview] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  // Poll a running scan; refresh the campaign when it lands.
  useEffect(() => {
    if (!job || job.status !== 'running') return
    const t = setInterval(async () => {
      try {
        const j = await api.discoverStatus(job.job_id)
        setJob(j)
        if (j.status !== 'running') onDone()
      } catch (e) { setJob(null); setError(e.message) }
    }, 2000)
    return () => clearInterval(t)
  }, [job?.job_id, job?.status])

  const d = discovery || {}
  const unscanned = d.unscanned_accounts || 0
  const running = job?.status === 'running' || d.running

  async function doPreview() {
    setBusy('preview'); setError(null)
    try { setPreview(await api.discoverAccounts(campaignId, { dry_run: true, limit })) }
    catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  async function doScan() {
    setBusy('scan'); setError(null); setPreview(null)
    try { setJob(await api.discoverAccounts(campaignId, { limit })) }
    catch (e) {
      setError(e.status === 409 ? 'A scan is already running for this campaign.' : e.message)
    } finally { setBusy(null) }
  }

  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="card-h">
        <div>
          <h3>Find accounts <Addon id="technographic-agent" /> <Addon id="hiring-agent" /></h3>
          <div className="card-note">
            {d.error
              ? `Scope unavailable — ${d.error}`
              : unscanned > 0
                ? <><b>{num(unscanned)}</b> in-scope accounts have never been scanned for signal.
                  Scanning them is how this campaign finds more.</>
                : 'Every in-scope account has been scanned. New signal arrives as accounts change.'}
          </div>
        </div>
        <div className="card-meta">
          {d.last_run_at
            ? <>Last scan {d.last_run_at.slice(0, 10)}</>
            : <>Never scanned</>}
          <div>
            {d.interval_days === 0
              ? 'Auto-scan off'
              : `Re-checks every ${d.interval_days ?? 7} days`}
            {d.due && !running && <span style={{ color: 'var(--amber)' }}> · due now</span>}
          </div>
        </div>
      </div>

      <ErrorBanner error={error} />

      <div className="card-actions">
        <label className="f">
          Scan up to{' '}
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))} disabled={running}>
            {PRESETS.map((n) => <option key={n} value={n}>{n} accounts</option>)}
          </select>
        </label>
        <button className="ghost sm" disabled={busy || running || !unscanned} onClick={doPreview}>
          {busy === 'preview' ? <Spinner /> : 'Preview'}
        </button>
        {/* No count on the button. It was a prediction of what WOULD be read, and
            the number that matters is what the scan actually found — which is the
            list below, once it has run. */}
        <button className="primary sm" disabled={busy || running || !unscanned} onClick={doScan}
          title="Runs the tech and hiring detectors. Hiring costs one Prospeo credit per account.">
          {busy === 'scan' ? <Spinner /> : 'Scan for signal'}
        </button>
        <span className="hint">Hiring lookups cost one credit each. Account news is researched later, during copy generation.</span>
      </div>

      {running && (
        <div className="hint" style={{ marginTop: 14 }}>
          <Spinner label={job?.total
            ? `Scanning ${job.done}/${job.total}${job.current ? ` — ${job.current}` : ''}`
            : 'Starting scan…'} />
        </div>
      )}

      {job && job.status === 'done' && <ScanResult job={job} />}
      {job && job.status === 'error' && (
        <div className="banner error" style={{ marginTop: 12, marginBottom: 0 }}>
          Scan failed: {job.error}
        </div>
      )}

      {preview && (
        <div style={{ marginTop: 12 }}>
          <div className="hint" style={{ marginBottom: 8 }}>
            {num(preview.count)} accounts would be scanned, most contacts first.
            {preview.costs_credits && ' This would spend that many Prospeo credits.'}
          </div>
          <div style={{ maxHeight: 200, overflowY: 'auto' }}>
            <table className="dense" style={{ width: '100%' }}>
              <tbody>
                {(preview.candidates || []).map((x) => (
                  <tr key={x.domain}>
                    <td className="mono" style={{ fontSize: 12 }}>{x.domain}</td>
                    <td className="muted" style={{ fontSize: 12 }}>{x.company || '—'}</td>
                    <td className="muted" style={{ fontSize: 12 }}>{x.contacts} contacts</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}


// What the scan actually found, account by account.
//
// The summary line alone ("scanned 25 accounts, 7 hiring") is a receipt for work
// done, not an answer — the rep's next question is always *which* accounts, and
// what was found at each. So the list is the result and the counts are the header.
//
// Accounts where the detectors found nothing are shown too, collapsed. "We looked
// and there was nothing" is a real outcome worth seeing once: it is why those
// accounts will not be re-scanned, and it stops a thin result reading as a bug.
function ScanResult({ job }) {
  const [showEmpty, setShowEmpty] = useState(false)
  const rows = job.results || []
  const found = rows.filter((r) => r.any)
  const empty = rows.filter((r) => !r.any)
  const failed = rows.filter((r) => r.error)

  return (
    <div style={{ marginTop: 14 }}>
      <div className="card-h" style={{ marginBottom: 8 }}>
        <div>
          <b style={{ fontSize: 13 }}>
            {found.length > 0
              ? `Signal at ${num(found.length)} of ${num(job.scanned)} accounts`
              : `Scanned ${num(job.scanned)} accounts — nothing detected`}
          </b>
          <div className="card-note">
            {job.qualified?.added
              ? <><b>{num(job.qualified.added)}</b> new contacts qualified into this
                campaign.</>
              : 'No new contacts qualified — the accounts that fired may already be '
                + 'members, or fall outside the audience.'}
          </div>
        </div>
        {Object.keys(job.detected || {}).length > 0 && (
          <div className="card-meta">
            {Object.entries(job.detected).map(([k, v]) => (
              <div key={k}>{num(v)} {k}</div>
            ))}
          </div>
        )}
      </div>

      {found.length > 0 && (
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          <table className="dense" style={{ width: '100%' }}>
            <thead><tr>
              <th style={{ width: '26%' }}>Account</th>
              <th style={{ width: '10%' }}>Contacts</th>
              <th style={{ width: '64%' }}>What we found</th>
            </tr></thead>
            <tbody>
              {found.map((r) => (
                <tr key={r.domain}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{r.company || r.domain}</div>
                    <div className="muted mono" style={{ fontSize: 11 }}>{r.domain}</div>
                  </td>
                  <td className="muted">{r.contacts != null ? num(r.contacts) : '—'}</td>
                  <td>
                    {Object.entries(r.found).map(([kind, line]) => (
                      <div key={kind} style={{ marginBottom: 2 }}>
                        <span className="badge">{kind}</span>{' '}
                        <span className="muted" style={{ fontSize: 12 }}>{line}</span>
                      </div>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {empty.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <button className="ghost sm" onClick={() => setShowEmpty((v) => !v)}>
            {showEmpty ? 'Hide' : 'Show'} {num(empty.length)} with nothing detected
          </button>
          {showEmpty && (
            <div className="hint" style={{ marginTop: 6 }}>
              {empty.map((r) => r.company || r.domain).join(' · ')}
              <div style={{ marginTop: 4 }}>
                These won't be re-scanned until their refresh window elapses — that
                is what the negative result buys.
              </div>
            </div>
          )}
        </div>
      )}

      {Object.entries(job.unavailable || {}).map(([k, why]) => (
        <div key={k} className="hint" style={{ marginTop: 8 }}>{k}: {why}</div>
      ))}
      {failed.length > 0 && (
        <div className="hint" style={{ marginTop: 6, color: 'var(--amber)' }}>
          {num(failed.length)} account(s) failed to scan — first: {job.errors?.[0]}
        </div>
      )}
    </div>
  )
}
