import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Badge, Spinner, ErrorBanner } from './ui.jsx'

// Slide-over drawer for one cached account: the full research signal, the
// technographic detection breakdown (per-vendor source/confidence/evidence +
// scan metadata + HubSpot write-back), the hiring scan (open roles + the
// sales/GTM subset that feeds email 2), and the contacts reusing this row.
// onChanged(payload) hands the parent the fresh signals payload after an action
// so the list updates in place (refresh/detect both return the full payload).
const NO_TECH = 'No signals detected'
const NO_HIRING = 'No open roles detected'

function pct(x) { return x == null ? '—' : `${Math.round(x * 100)}%` }

function hubspotLine(hs, envVar = 'TECH_HUBSPOT_WRITEBACK') {
  if (!hs) return 'not attempted'
  if (hs.ok) return `updated company ${hs.company_id}`
  if (hs.reason === 'disabled') return `disabled (${envVar}=0)`
  if (hs.reason === 'not_matched') return 'skipped (company not matched by Prospeo)'
  if (hs.reason === 'no_company') return 'no matching company in HubSpot'
  if (hs.reason === 'no_token') return 'no HubSpot token configured'
  return hs.error ? `error: ${hs.error}` : 'not written'
}

export default function SignalDetail({ domain, onClose, onChanged }) {
  const [d, setD] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(null) // 'refresh' | 'detect' | 'hiring'

  function load() {
    setError(null)
    api.signalDetail(domain).then(setD).catch((e) => setError(e.message))
  }
  useEffect(() => { setD(null); load() }, [domain])

  async function act(kind) {
    setBusy(kind); setError(null)
    try {
      const payload = kind === 'refresh'
        ? await api.refreshSignal(domain)
        : kind === 'hiring'
          ? await api.detectHiring(domain, true)
          : await api.detectTech(domain, true)
      if (payload && payload.ok === false) setError(payload.error || `${kind} failed`)
      else if (onChanged && payload?.signals) onChanged(payload)
      load() // re-pull this drawer's detail (tech_detail, hiring_detail, contacts)
    } catch (e) { setError(e.message) }
    finally { setBusy(null) }
  }

  const s = d?.signal
  const td = s?.tech_detail
  const dets = td?.detections || []
  const hd = s?.hiring_detail
  const salesTitles = hd?.sales_titles || []
  const allTitles = hd?.active_titles || []
  const contacts = d?.contacts || []
  const techOff = d ? d.tech_available === false : false
  const hiringOff = d ? d.hiring_available === false : false

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer">
        <button className="close-x" onClick={onClose}>Close ✕</button>
        <ErrorBanner error={error} />
        {!d && !error && <Spinner label="Loading…" />}
        {d && !s && !error && <div className="empty">No cached signal for {domain}.</div>}
        {s && (
          <>
            <h2 className="page-title mono" style={{ marginBottom: 2 }}>{s.domain}</h2>
            <p className="page-sub" style={{ marginBottom: 14 }}>
              {s.company_name || <span className="muted">no company name</span>}
            </p>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              <span className="badge" style={{ color: s.has_recent ? 'var(--green)' : 'var(--amber)', borderColor: s.has_recent ? 'var(--green)' : 'var(--amber)' }}>
                {s.has_recent ? 'recent signal' : 'fallback'}
              </span>
              {s.age_days != null && (
                <span className="badge" style={{ color: s.fresh ? 'var(--muted)' : 'var(--red)' }}>
                  researched {s.age_days}d ago{s.fresh ? '' : ' · stale'}
                </span>
              )}
              {s.tech_signals && s.tech_signals !== NO_TECH
                ? <span className="badge" style={{ color: 'var(--jade)', borderColor: 'var(--jade)' }}>{dets.length} tech</span>
                : s.tech_signals === NO_TECH
                  ? <span className="badge muted">no tech detected</span>
                  : s.tech_error
                    ? <span className="badge" style={{ color: 'var(--red)', borderColor: 'var(--red)' }}>scan failed</span>
                    : <span className="badge muted">not scanned</span>}
              {s.hiring_signals && s.hiring_signals !== NO_HIRING
                ? <span className="badge" style={{ color: 'var(--jade)', borderColor: 'var(--jade)' }}>
                    {salesTitles.length ? `${salesTitles.length} sales roles open` : 'open roles'}
                  </span>
                : s.hiring_signals === NO_HIRING
                  ? <span className="badge muted">no open roles</span>
                  : s.hiring_error
                    ? <span className="badge" style={{ color: 'var(--red)', borderColor: 'var(--red)' }}>hiring scan failed</span>
                    : <span className="badge muted">hiring not scanned</span>}
            </div>

            <div className="row" style={{ gap: 8, marginTop: 14 }}>
              <button className="ghost sm" disabled={busy === 'refresh'} onClick={() => act('refresh')}>
                {busy === 'refresh' ? <Spinner /> : '↻ Refresh signal'}
              </button>
              <button className="ghost sm" disabled={techOff || busy === 'detect'} onClick={() => act('detect')}
                title={techOff ? (d.tech_reason || 'detection unavailable') : 'Re-scan website + DNS'}>
                {busy === 'detect' ? <Spinner /> : '⌁ Detect tech'}
              </button>
              <button className="ghost sm" disabled={hiringOff || busy === 'hiring'} onClick={() => act('hiring')}
                title={hiringOff ? (d.hiring_reason || 'detection unavailable') : 'Check live job postings (one Prospeo credit)'}>
                {busy === 'hiring' ? <Spinner /> : '⚑ Detect hiring'}
              </button>
            </div>

            <div className="section-h">Signal</div>
            <div className="touch"><div className="body">{s.signal || <span className="muted">—</span>}</div></div>
            <div className="kv">
              <span className="k">Researched</span>
              <span>{s.researched_at ? `${s.researched_at}${s.age_days != null ? ` (${s.age_days}d ago)` : ''}` : '—'}</span>
              {s.model && (<><span className="k">Model</span><span className="mono">{s.model}</span></>)}
            </div>

            <div className="section-h">Technographics</div>
            {s.tech_signals && s.tech_signals !== NO_TECH ? (
              <>
                <div className="touch"><div className="body">{s.tech_signals}</div></div>
                {dets.length > 0 && (
                  <table className="dense" style={{ marginBottom: 14 }}>
                    <thead><tr><th>Vendor</th><th>Bucket</th><th>Src</th><th>Conf</th><th>Evidence</th></tr></thead>
                    <tbody>
                      {dets.map((x) => (
                        <tr key={x.vendor_id}>
                          <td>{x.vendor_name}{x.tier_signal ? <span className="badge cta" style={{ marginLeft: 6 }}>{x.tier_signal}</span> : null}</td>
                          <td className="muted">{x.bucket || <span className="muted">—</span>}</td>
                          <td className="muted">{x.source}</td>
                          <td>{pct(x.confidence)}</td>
                          <td className="mono muted" style={{ fontSize: 11, maxWidth: 220, whiteSpace: 'pre-wrap' }}>
                            {(x.evidence || []).join('\n') || '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            ) : s.tech_signals === NO_TECH ? (
              <p className="muted" style={{ marginBottom: 12 }}>Scan ran, no marketing/sales tech matched.</p>
            ) : s.tech_error ? (
              <div className="touch"><div className="body" style={{ color: 'var(--red)' }}>Scan failed: {s.tech_error}</div></div>
            ) : (
              <p className="muted" style={{ marginBottom: 12 }}>Not scanned yet. Use Detect tech above.</p>
            )}
            {td && (
              <div className="kv">
                {s.tech_checked_at && (<><span className="k">Scanned</span><span>{s.tech_checked_at}{s.tech_age_days != null ? ` (${s.tech_age_days}d ago)` : ''}</span></>)}
                <span className="k">DNS records</span><span>{td.dns_records ?? '—'}</span>
                <span className="k">Duration</span><span>{td.duration_ms != null ? `${td.duration_ms} ms` : '—'}</span>
                <span className="k">Selection</span><span className="mono">{td.selection || '—'}</span>
                {td.rendered && (<><span className="k">Mode</span><span>rendered (Playwright)</span></>)}
                {td.fetch_error && (<><span className="k">Fetch</span><span style={{ color: 'var(--amber)' }}>{td.fetch_error}</span></>)}
                {td.dns_error && (<><span className="k">DNS</span><span style={{ color: 'var(--amber)' }}>{td.dns_error}</span></>)}
                <span className="k">HubSpot</span><span>{hubspotLine(td.hubspot)}</span>
              </div>
            )}

            <div className="section-h">Hiring</div>
            {s.hiring_signals && s.hiring_signals !== NO_HIRING ? (
              <>
                <div className="touch"><div className="body">{s.hiring_signals}</div></div>
                {salesTitles.length > 0 && (
                  <p className="muted" style={{ marginBottom: 10 }}>
                    <span style={{ color: 'var(--jade)' }}>Sales/GTM roles ({salesTitles.length}):</span>{' '}
                    {salesTitles.join('; ')}
                  </p>
                )}
                {allTitles.length > 0 && (
                  <p className="muted" style={{ marginBottom: 12, fontSize: 12 }}>
                    All open roles ({allTitles.length}): {allTitles.slice(0, 40).join('; ')}
                    {allTitles.length > 40 ? ` (+${allTitles.length - 40} more)` : ''}
                  </p>
                )}
              </>
            ) : s.hiring_signals === NO_HIRING ? (
              <p className="muted" style={{ marginBottom: 12 }}>
                {hd?.error_code
                  ? `Not matched by Prospeo (${hd.error_code}) — treated as no data.`
                  : 'Scan ran, no open job postings found.'}
              </p>
            ) : s.hiring_error ? (
              <div className="touch"><div className="body" style={{ color: 'var(--red)' }}>Scan failed: {s.hiring_error}</div></div>
            ) : (
              <p className="muted" style={{ marginBottom: 12 }}>Not scanned yet. Use Detect hiring above.</p>
            )}
            {hd && (
              <div className="kv">
                {s.hiring_checked_at && (<><span className="k">Checked</span><span>{s.hiring_checked_at}{s.hiring_age_days != null ? ` (${s.hiring_age_days}d ago)` : ''}</span></>)}
                <span className="k">Active roles</span><span>{hd.active_count ?? '—'}</span>
                <span className="k">Sales matches</span><span>{salesTitles.length}</span>
                {hd.error_code && (<><span className="k">Error code</span><span className="mono">{hd.error_code}</span></>)}
                <span className="k">Duration</span><span>{hd.duration_ms != null ? `${hd.duration_ms} ms` : '—'}</span>
                <span className="k">HubSpot</span><span>{hubspotLine(hd.hubspot, 'HIRING_HUBSPOT_WRITEBACK')}</span>
              </div>
            )}

            <div className="section-h">Contacts at this domain ({contacts.length})</div>
            {contacts.length === 0 ? (
              <p className="muted">No contacts reference this domain.</p>
            ) : (
              <table className="dense">
                <thead><tr><th>Name</th><th>Title</th><th>Persona</th><th>Status</th><th>Batch</th></tr></thead>
                <tbody>
                  {contacts.map((c) => (
                    <tr key={c.contact_id}>
                      <td>{`${c.first_name || ''} ${c.last_name || ''}`.trim() || <span className="muted">—</span>}</td>
                      <td className="muted" style={{ maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c.title}>{c.title || '—'}</td>
                      <td>{c.persona ? <Badge kind="persona" value={c.persona} /> : <span className="muted">—</span>}</td>
                      <td>{c.status ? <Badge kind="status" value={c.status} /> : <span className="muted">—</span>}</td>
                      <td className="muted">{c.batch_id != null ? `#${c.batch_id}` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>
    </>
  )
}
