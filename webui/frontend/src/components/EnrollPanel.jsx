import { useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import OutreachDetail from './OutreachDetail.jsx'

// campaigns now route by variant (14/15/16); 10-13 are the legacy persona campaigns
const CAMPAIGN_PERSONA = {
  14: 'value-give', 15: 'earn', 16: 'show',
  10: 'sales-leadership', 11: 'revops', 12: 'partnerships', 13: 'sdr-bdr',
}

// Enrollment with a dry-run gate: always preview first, then a confirm modal
// before the live write to Bison. Each preview row opens the full generated copy.
export default function EnrollPanel({ generatedReady, onChanged }) {
  const [preview, setPreview] = useState(null)
  const [result, setResult] = useState(null)
  const [openId, setOpenId] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [confirming, setConfirming] = useState(false)
  const [ack, setAck] = useState(false)

  async function runDryRun() {
    setBusy(true); setError(null); setResult(null); setPreview(null)
    try { setPreview(await api.enrollDryRun()) }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  async function runLive() {
    setBusy(true); setError(null)
    try {
      const r = await api.enrollLive()
      setResult(r); setPreview(null); setConfirming(false); setAck(false)
      if (!r.ok) setError('Enrollment script returned a non-zero exit — see counts/log below.')
      onChanged?.()
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  const byCampaign = preview?.by_campaign || {}

  return (
    <div className="panel">
      <div className="row between">
        <span className="section-h" style={{ margin: 0 }}>Enrollment → Email campaign</span>
        <span className="badge">{num(generatedReady)} generated ready</span>
      </div>

      <ErrorBanner error={error} />

      {generatedReady === 0 && !result && (
        <div className="banner info" style={{ marginTop: 12 }}>
          Nothing to enroll right now — enrollment activates once batches generate copy
          (contacts in <span className="mono">generated</span> status).
        </div>
      )}

      <div className="row" style={{ gap: 12, marginTop: 14 }}>
        <button className="ghost" onClick={runDryRun} disabled={busy || generatedReady === 0}>
          {busy && !confirming ? <Spinner label="Previewing…" /> : 'Preview enrollment (dry-run)'}
        </button>
        {preview && preview.rows.length > 0 && (
          <button onClick={() => setConfirming(true)} disabled={busy}>
            Enroll {num(preview.rows.length)} live →
          </button>
        )}
      </div>

      {/* dry-run preview */}
      {preview && (
        <div style={{ marginTop: 16 }}>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
            <span className="muted">Planned routing:</span>
            {Object.entries(byCampaign).map(([c, n]) => (
              <span key={c} className="badge">campaign {c} ({CAMPAIGN_PERSONA[c] || '?'}): {n}</span>
            ))}
            {preview.rows.length === 0 && <span className="muted">No contacts in <span className="mono">generated</span> status.</span>}
          </div>
          {preview.rows.length > 0 && (
            <>
              <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
                Click a row to review the signal and the full copy before enrolling.
              </div>
              <div className="panel" style={{ padding: 0, maxHeight: 360, overflow: 'auto' }}>
                <table>
                  <thead><tr><th>Contact</th><th>Persona</th><th>CTA</th><th>Signal</th><th>Campaign</th></tr></thead>
                  <tbody>
                    {preview.rows.slice(0, 200).map((r, i) => (
                      <tr key={i} className={r.contact_id ? 'clickable' : ''}
                          onClick={() => r.contact_id && setOpenId(r.contact_id)}>
                        <td>
                          <div>{[r.first_name, r.last_name].filter(Boolean).join(' ') || '—'}</div>
                          <div className="muted" style={{ fontSize: 11 }}>{r.company || r.email}</div>
                        </td>
                        <td><span className={`badge persona-${r.persona}`}>{r.persona}</span></td>
                        <td>{r.cta_type ? <span className="badge cta">{r.cta_type}</span> : '—'}</td>
                        <td className="muted" style={{ maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {r.signal || '—'}</td>
                        <td>{r.campaign}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      {/* live result */}
      {result && (
        <div style={{ marginTop: 16 }}>
          <div className="banner info">
            Enrolled <b>{num(result.counts?.enrolled || 0)}</b> ·
            skipped <b>{num(result.counts?.skipped || 0)}</b> ·
            no-campaign <b>{num(result.counts?.no_campaign || 0)}</b> ·
            missing-file <b>{num(result.counts?.missing_file || 0)}</b>
          </div>
          {result.rows.filter((r) => r.skipped).length > 0 && (
            <div className="panel" style={{ padding: 0, marginTop: 12, maxHeight: 220, overflow: 'auto' }}>
              <table>
                <thead><tr><th>Skipped</th><th>Reason</th></tr></thead>
                <tbody>
                  {result.rows.filter((r) => r.skipped).map((r, i) => (
                    <tr key={i}><td className="mono">{r.email}</td><td style={{ color: 'var(--amber)' }}>{r.reason}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* confirm modal */}
      {confirming && (
        <>
          <div className="drawer-backdrop" onClick={() => !busy && setConfirming(false)} />
          <div className="panel" style={{
            position: 'fixed', top: '30%', left: '50%', transform: 'translateX(-50%)',
            zIndex: 50, width: 460, maxWidth: '92vw', boxShadow: 'var(--shadow-md)',
          }}>
            <h2 style={{ marginTop: 0 }}>Confirm live enrollment</h2>
            <p className="muted">
              This sends <b>{num(preview?.rows.length || 0)}</b> leads live to the email campaign
              (campaigns {Object.keys(byCampaign).join(', ')}). This is an outward action and cannot be undone.
            </p>
            <label className="row" style={{ gap: 8, margin: '14px 0' }}>
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} style={{ width: 'auto' }} />
              <span>I understand this writes live to the email campaign.</span>
            </label>
            <div className="row" style={{ gap: 10, justifyContent: 'flex-end' }}>
              <button className="ghost" onClick={() => setConfirming(false)} disabled={busy}>Cancel</button>
              <button onClick={runLive} disabled={!ack || busy} style={{ background: 'var(--red)' }}>
                {busy ? <Spinner label="Enrolling…" /> : 'Enroll live now'}
              </button>
            </div>
          </div>
        </>
      )}

      {openId && <OutreachDetail id={openId} onClose={() => setOpenId(null)} />}
    </div>
  )
}
