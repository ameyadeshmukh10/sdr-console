import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from '../components/ui.jsx'

const CAMPAIGNS = [
  { id: '', label: 'All campaigns' },
  { id: '10', label: '10 · sales-leadership' },
  { id: '11', label: '11 · revops' },
  { id: '12', label: '12 · partnerships' },
  { id: '13', label: '13 · sdr-bdr' },
]
const INTENT_COLOR = {
  meeting_request: 'var(--green)', info_request: 'var(--accent)', pricing: 'var(--purple)',
  positive_later: 'var(--green)', positive_other: 'var(--green)', referral: 'var(--amber)',
  not_interested: 'var(--muted)', auto_reply: 'var(--muted)', unsubscribe: 'var(--red)', error: 'var(--red)',
}

// Replies — one flow: scan the Bison inbox → Claude classifies (junk/auto-replies
// filtered, opt-outs auto-suppressed) → each interested reply gets an AI follow-up
// draft you edit and approve. Approving tags the lead Interested and queues the
// follow-up into the Interested Follow-up campaign (which sends it once active).
export default function RepliesPage() {
  const [queue, setQueue] = useState(null)
  const [drafts, setDrafts] = useState(null)
  const [error, setError] = useState(null)        // load errors only
  const [msg, setMsg] = useState(null)            // unified action feedback {err, text}
  const [scanning, setScanning] = useState(false)
  const [drafting, setDrafting] = useState(false)
  const [busy, setBusy] = useState(null)          // reply_id being approved
  const [lookback, setLookback] = useState(14)
  const [campaign, setCampaign] = useState('')
  const [edits, setEdits] = useState({})          // reply_id -> edited draft
  const [showOthers, setShowOthers] = useState(false)
  const [samples, setSamples] = useState(null)
  const [samplesBusy, setSamplesBusy] = useState(null)

  const loadQueue = () => api.repliesQueue().then(setQueue).catch((e) => setError(e.message))
  const loadDrafts = () => api.followupDrafts().then(setDrafts).catch(() => {})
  useEffect(() => { loadQueue(); loadDrafts() }, [])

  async function scan() {
    setScanning(true); setError(null); setMsg(null)
    try {
      const q = await api.scanReplies({ lookback_days: Number(lookback), campaign_id: campaign ? Number(campaign) : null })
      if (q.ok === false) setMsg({ err: true, text: 'Scan returned errors — see results.' })
      await Promise.all([loadQueue(), loadDrafts()])
    } catch (e) { setError(e.message) }
    finally { setScanning(false) }
  }

  async function generateDrafts() {
    setDrafting(true); setMsg(null)
    try { await api.draftFollowups(); await loadDrafts() }
    catch (e) { setMsg({ err: true, text: e.message }) }
    finally { setDrafting(false) }
  }

  async function approve(it) {
    setBusy(it.reply_id); setMsg(null)
    try {
      const r = await api.approveFollowup(it.reply_id, edits[it.reply_id] ?? draftFor(it)?.draft)
      if (r.ok === false) setMsg({ err: true, text: r.error || 'approve failed' })
      else setMsg({ err: false, text: `${it.from_name || it.from_email}: tagged Interested and queued into the Interested Follow-up campaign.` })
      await Promise.all([loadDrafts(), loadQueue()])
    } catch (e) { setMsg({ err: true, text: e.message }) }
    finally { setBusy(null) }
  }

  async function draftSamples(it) {
    setSamplesBusy(it.reply_id); setSamples(null)
    try { setSamples({ ...(await api.samples({ from_email: it.from_email })), from_name: it.from_name }) }
    catch (e) { setMsg({ err: true, text: e.message }) }
    finally { setSamplesBusy(null) }
  }

  const items = queue?.items || []
  const counts = queue?.counts || {}
  const draftBy = useMemo(
    () => Object.fromEntries((drafts?.items || []).map((d) => [String(d.reply_id), d])), [drafts])
  const draftFor = (it) => draftBy[String(it.reply_id)]
  const interested = items.filter((it) => it.classifier?.interested)
  const others = items.filter((it) => !it.classifier?.interested)
  const needDrafts = interested.filter((it) => !draftFor(it) && !it.already_interested).length

  return (
    <div>
      <h1 className="page-title">Replies</h1>
      <p className="page-sub">
        Scan the Bison inbox — Claude classifies each reply, auto-replies / non-lead senders / test mail are
        filtered out, and explicit opt-outs are unsubscribed automatically. Review each interested reply, edit its
        AI follow-up, and approve to tag the lead Interested and queue the follow-up into the Interested Follow-up campaign.
      </p>

      <div className="panel" style={{ marginBottom: 18 }}>
        <div className="toolbar" style={{ marginBottom: 0 }}>
          <label className="field">Lookback (days)
            <input type="number" min="1" max="90" value={lookback} onChange={(e) => setLookback(e.target.value)} style={{ width: 90 }} />
          </label>
          <label className="field">Campaign
            <select value={campaign} onChange={(e) => setCampaign(e.target.value)}>
              {CAMPAIGNS.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
          </label>
          <button onClick={scan} disabled={scanning}>
            {scanning ? <Spinner label="Scanning + classifying…" /> : '⟲ Scan inbox'}
          </button>
          {queue?.scanned_at && <span className="muted" style={{ alignSelf: 'center' }}>last scan {new Date(queue.scanned_at).toLocaleString()}</span>}
        </div>
      </div>

      <ErrorBanner error={error} />
      {msg && <div className={`banner ${msg.err ? 'warn' : 'info'}`}>{msg.text}</div>}

      {queue && (
        <div className="grid stat-grid" style={{ marginBottom: 22 }}>
          <Stat label="Scanned" value={num(counts.scanned || 0)} sub={`last ${queue.lookback_days || 14}d`} />
          <Stat label="Interested" value={num(interested.length)} sub="to follow up" />
          <Stat label="Filtered out" value={num(counts.filtered || 0)} sub="auto-reply / non-lead / test" />
          <Stat label="Unsubscribed" value={num(counts.unsubscribed || 0)} sub="opt-outs, suppressed in Bison" />
        </div>
      )}

      {!queue ? <Spinner label="Loading…" /> : (
        <>
          {/* Interested replies — the work */}
          <div className="row between" style={{ marginBottom: 10 }}>
            <h2 className="section-h" style={{ margin: 0 }}>Interested replies <span className="muted">({num(interested.length)})</span></h2>
            {needDrafts > 0 && (
              <button onClick={generateDrafts} disabled={drafting}>
                {drafting ? <Spinner label="Drafting…" /> : `✎ Draft follow-ups (${needDrafts})`}
              </button>
            )}
          </div>

          {interested.length === 0 ? (
            <div className="empty">No interested replies in this window. Adjust the lookback and <b>Scan inbox</b>.</div>
          ) : (
            <div className="grid" style={{ gap: 12 }}>
              {interested.map((it) => {
                const cls = it.classifier || {}
                const d = draftFor(it)
                // "queued" = the follow-up was approved into the campaign. Being
                // already tagged Interested is separate — you can still follow up.
                const queued = d?.status === 'queued' || d?.status === 'sent'
                return (
                  <div className="panel" key={it.reply_id} style={{ opacity: queued ? 0.75 : 1 }}>
                    <div className="row between" style={{ marginBottom: 6 }}>
                      <span>
                        <b>{it.from_name || it.from_email}</b>
                        <span className="muted" style={{ fontSize: 12, marginLeft: 6 }}>{it.from_email}</span>
                      </span>
                      <span className="row" style={{ gap: 8, alignItems: 'center' }}>
                        <span className="badge" style={{ color: INTENT_COLOR[cls.intent] || 'var(--muted)' }}>{cls.intent}</span>
                        {cls.confidence != null && <span className="muted" style={{ fontSize: 12 }}>{Math.round(cls.confidence * 100)}%</span>}
                        {it.already_interested && <span className="badge status-enrolled">tagged</span>}
                        {queued && <span className="badge" style={{ color: 'var(--green)', borderColor: 'var(--green)' }}>queued ✓</span>}
                      </span>
                    </div>
                    {it.subject && <div className="muted" style={{ fontSize: 12 }}>{it.subject}</div>}
                    <div style={{ fontSize: 13, margin: '6px 0 10px', borderLeft: '2px solid var(--border)', paddingLeft: 10, whiteSpace: 'pre-wrap' }}>
                      {it.text_body}
                    </div>

                    {/* the AI follow-up */}
                    {d?.error ? (
                      <div className="banner warn">Draft error: {d.error}</div>
                    ) : d ? (
                      <>
                        <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>AI follow-up draft — edit before approving:</div>
                        <textarea value={edits[it.reply_id] ?? d.draft} disabled={queued}
                          onChange={(e) => setEdits((m) => ({ ...m, [it.reply_id]: e.target.value }))}
                          rows={5} style={{ width: '100%', boxSizing: 'border-box', fontSize: 13 }} />
                        {d.rationale && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>why: {d.rationale}</div>}
                      </>
                    ) : (
                      <div className="muted" style={{ fontSize: 12 }}>No draft yet — click <b>Draft follow-ups</b> above.</div>
                    )}

                    <div className="row between" style={{ marginTop: 10, alignItems: 'center' }}>
                      <button className="linklike" style={{ fontSize: 12 }}
                        onClick={() => draftSamples(it)} disabled={samplesBusy === it.reply_id}
                        title="Draft 3 sample emails our AI would write for this lead's company">
                        {samplesBusy === it.reply_id ? <Spinner /> : 'Draft 3 samples'}
                      </button>
                      {!queued && d && !d.error && (
                        <button onClick={() => approve(it)} disabled={busy === it.reply_id} style={{ background: 'var(--green)' }}>
                          {busy === it.reply_id ? <Spinner label="Approving…" /> : 'Approve follow-up →'}
                        </button>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* Everything else — collapsed, low-priority */}
          {others.length > 0 && (
            <div style={{ marginTop: 26 }}>
              <button className="linklike" onClick={() => setShowOthers((v) => !v)} style={{ fontSize: 13 }}>
                {showOthers ? '▾' : '▸'} {num(others.length)} other replies (not interested)
              </button>
              {showOthers && (
                <div className="panel" style={{ padding: 0, marginTop: 8 }}>
                  <table>
                    <thead><tr><th>From</th><th>Intent</th><th>Reason</th><th>Conf.</th></tr></thead>
                    <tbody>
                      {others.map((it) => {
                        const cls = it.classifier || {}
                        return (
                          <tr key={it.reply_id}>
                            <td>{it.from_name || it.from_email}<div className="muted" style={{ fontSize: 11 }}>{it.from_email}</div></td>
                            <td><span className="badge" style={{ color: INTENT_COLOR[cls.intent] || 'var(--muted)' }}>{cls.intent}</span></td>
                            <td className="muted" style={{ fontSize: 12, maxWidth: 360 }}>{cls.reason}</td>
                            <td>{cls.confidence != null ? `${Math.round(cls.confidence * 100)}%` : '—'}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </>
      )}

      {samples && (
        <>
          <div className="drawer-backdrop" onClick={() => setSamples(null)} />
          <div className="drawer">
            <button className="close-x" onClick={() => setSamples(null)}>Close ✕</button>
            <h2 className="page-title" style={{ marginBottom: 2 }}>3 samples for {samples.company || '—'}</h2>
            <p className="page-sub" style={{ marginBottom: 12 }}>
              What our AI SDR would write for {samples.from_name ? `${samples.from_name}'s` : 'their'} own outbound.
              Paste these into your reply to prove the product.
            </p>
            <div className="banner info"><b>Their ICP:</b> {samples.icp_summary || '—'}</div>
            {(samples.samples || []).map((s, i) => (
              <div className="touch" key={i}>
                <div className="step">Sample {i + 1} · {s.account}</div>
                {s.signal && <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>signal: {s.signal}</div>}
                <div className="body">{s.opener}</div>
              </div>
            ))}
            {(samples.samples || []).length === 0 && <div className="empty">No samples returned — try again.</div>}
          </div>
        </>
      )}
    </div>
  )
}
