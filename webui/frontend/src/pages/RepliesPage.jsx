import { useEffect, useMemo, useRef, useState } from 'react'
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
const MIN_CONF = 0.50  // only interested/referral above this confidence are surfaced

// Collapsible list of the outbound sequence emails we sent this prospect.
function SentEmails({ emails }) {
  const [open, setOpen] = useState(false)
  if (!emails?.length) return null
  return (
    <div style={{ margin: '4px 0 8px' }}>
      <button className="linklike" style={{ fontSize: 12 }} onClick={() => setOpen((v) => !v)}>
        {open ? '▾' : '▸'} Emails we sent ({emails.length})
      </button>
      {open && emails.map((m, i) => (
        <div key={i} style={{ borderLeft: '2px solid var(--border)', paddingLeft: 10, margin: '6px 0', fontSize: 12 }}>
          <div className="muted">{m.subject || '(no subject)'}{m.date ? ` · ${new Date(m.date).toLocaleDateString()}` : ''}</div>
          <div style={{ whiteSpace: 'pre-wrap', color: 'var(--muted)' }}>{(m.text || '').slice(0, 600)}</div>
        </div>
      ))}
    </div>
  )
}

function ProgressBar({ pct }) {
  return (
    <div style={{ height: 8, background: 'var(--border)', borderRadius: 4, overflow: 'hidden' }}>
      <div style={{ height: '100%', width: `${pct}%`, background: 'var(--accent, #0a7)', transition: 'width .25s ease' }} />
    </div>
  )
}

// Replies — one loop: scan the Bison inbox → Claude classifies → review the possible
// interested/referrals (with the prospect's title + the emails we sent + the sending
// inbox) → Tag interested → draft the follow-up → Approve, which sends the reply
// directly in the prospect's Bison thread and clears the card. A re-reply reappears.
export default function RepliesPage() {
  const [queue, setQueue] = useState(null)
  const [drafts, setDrafts] = useState(null)
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)            // unified action feedback {err, text}
  const [scanning, setScanning] = useState(false)
  const [drafting, setDrafting] = useState(false)
  const [tagging, setTagging] = useState(null)    // reply_id being tagged
  const [busy, setBusy] = useState(null)          // reply_id being approved
  const [lookback, setLookback] = useState(14)
  const [campaign, setCampaign] = useState('')
  const [edits, setEdits] = useState({})          // reply_id -> edited draft
  const [showOthers, setShowOthers] = useState(false)
  const [pct, setPct] = useState({})              // reply_id -> send progress %
  const timers = useRef({})

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

  async function tag(it) {
    setTagging(it.reply_id); setMsg(null)
    try {
      const r = await api.tagReplies([it.reply_id])
      if (!r.ok) setMsg({ err: true, text: `Failed to tag ${it.from_name || it.from_email}.` })
      await loadQueue()
    } catch (e) { setMsg({ err: true, text: e.message }) }
    finally { setTagging(null) }
  }

  async function generateDrafts() {
    setDrafting(true); setMsg(null)
    try { await api.draftFollowups(); await loadDrafts() }
    catch (e) { setMsg({ err: true, text: e.message }) }
    finally { setDrafting(false) }
  }

  async function approve(it) {
    const id = it.reply_id
    setBusy(id); setMsg(null)
    setPct((p) => ({ ...p, [id]: 8 }))
    timers.current[id] = setInterval(
      () => setPct((p) => ({ ...p, [id]: Math.min((p[id] || 8) + 11, 90) })), 180)
    const stop = () => { clearInterval(timers.current[id]); delete timers.current[id] }
    try {
      const r = await api.approveFollowup(id, edits[id] ?? draftFor(it)?.draft)
      stop()
      if (r.ok === false) {
        setPct((p) => ({ ...p, [id]: 0 })); setMsg({ err: true, text: r.error || 'send failed' })
      } else {
        setPct((p) => ({ ...p, [id]: 100 }))
        setMsg({ err: false, text: `Sent follow-up to ${it.from_name || it.from_email} in the thread.` })
        setTimeout(() => { Promise.all([loadQueue(), loadDrafts()]); setPct((p) => { const n = { ...p }; delete n[id]; return n }) }, 850)
      }
    } catch (e) { stop(); setPct((p) => ({ ...p, [id]: 0 })); setMsg({ err: true, text: e.message }) }
    finally { setBusy(null) }
  }

  const items = queue?.items || []
  const counts = queue?.counts || {}
  const draftBy = useMemo(
    () => Object.fromEntries((drafts?.items || []).map((d) => [String(d.reply_id), d])), [drafts])
  const draftFor = (it) => draftBy[String(it.reply_id)]

  const isCandidate = (it) => it.classifier?.interested && (it.classifier.confidence || 0) > MIN_CONF && !it.handled
  const possible = items.filter((it) => isCandidate(it) && !it.already_interested)
  const tagged = items.filter((it) => it.already_interested && !it.handled
    && it.classifier?.interested && (it.classifier.confidence || 0) > MIN_CONF)
  const others = items.filter((it) => !isCandidate(it) && !(it.already_interested && it.classifier?.interested))
  const needDrafts = tagged.filter((it) => !draftFor(it)).length

  // Shared header block for a reply card: who, title, sending inbox, intent/conf.
  const CardHead = ({ it, cls, extra }) => (
    <>
      <div className="row between" style={{ marginBottom: 4 }}>
        <span>
          <b>{it.from_name || `${it.first_name || ''} ${it.last_name || ''}`.trim() || it.from_email}</b>
          <span className="muted" style={{ fontSize: 12, marginLeft: 6 }}>{it.from_email}</span>
        </span>
        <span className="row" style={{ gap: 8, alignItems: 'center' }}>
          <span className="badge" style={{ color: INTENT_COLOR[cls.intent] || 'var(--muted)' }}>{cls.intent}</span>
          {cls.confidence != null && <span className="muted" style={{ fontSize: 12 }}>{Math.round(cls.confidence * 100)}%</span>}
          {extra}
        </span>
      </div>
      {it.title && <div className="muted" style={{ fontSize: 12 }}>{it.title}</div>}
      {it.sending_email && <div className="muted" style={{ fontSize: 11 }}>sent from {it.sending_email}</div>}
      <SentEmails emails={it.sent_emails} />
      {it.subject && <div className="muted" style={{ fontSize: 12 }}>{it.subject}</div>}
      <div style={{ fontSize: 13, margin: '6px 0 10px', borderLeft: '2px solid var(--border)', paddingLeft: 10, whiteSpace: 'pre-wrap' }}>
        {it.text_body}
      </div>
    </>
  )

  return (
    <div>
      <h1 className="page-title">Replies</h1>
      <p className="page-sub">
        Scan the Bison inbox — Claude classifies each reply (auto-replies / non-lead senders / test mail filtered,
        opt-outs auto-unsubscribed). Review the possible interested replies & referrals, tag the real ones, draft the
        AI follow-up, and approve to send it straight back in the prospect's thread.
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
          <Stat label="Possible interested" value={num(possible.length)} sub={`> ${Math.round(MIN_CONF * 100)}% conf · to review`} />
          <Stat label="Tagged" value={num(tagged.length)} sub="draft + send" />
          <Stat label="Unsubscribed" value={num(counts.unsubscribed || 0)} sub="opt-outs, suppressed in Bison" />
        </div>
      )}

      {!queue ? <Spinner label="Loading…" /> : (
        <>
          {/* 1 — Possible interested: review + tag */}
          <h2 className="section-h" style={{ margin: '0 0 10px' }}>Possible interested <span className="muted">({num(possible.length)})</span></h2>
          {possible.length === 0 ? (
            <div className="empty">Nothing to review. Adjust the lookback and <b>Scan inbox</b>.</div>
          ) : (
            <div className="grid" style={{ gap: 12 }}>
              {possible.map((it) => {
                const cls = it.classifier || {}
                return (
                  <div className="panel" key={it.reply_id}>
                    <CardHead it={it} cls={cls} />
                    <div className="row" style={{ justifyContent: 'flex-end' }}>
                      <button onClick={() => tag(it)} disabled={tagging === it.reply_id} style={{ background: 'var(--green)' }}>
                        {tagging === it.reply_id ? <Spinner label="Tagging…" /> : 'Tag interested →'}
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* 2 — Interested replies: draft + approve (sends in-thread) */}
          <div className="row between" style={{ margin: '28px 0 10px' }}>
            <h2 className="section-h" style={{ margin: 0 }}>Interested replies <span className="muted">({num(tagged.length)})</span></h2>
            {needDrafts > 0 && (
              <button onClick={generateDrafts} disabled={drafting}>
                {drafting ? <Spinner label="Drafting…" /> : `✎ Draft follow-ups (${needDrafts})`}
              </button>
            )}
          </div>
          {tagged.length === 0 ? (
            <div className="empty">Tag a reply above to start a follow-up.</div>
          ) : (
            <div className="grid" style={{ gap: 12 }}>
              {tagged.map((it) => {
                const cls = it.classifier || {}
                const d = draftFor(it)
                const sending = busy === it.reply_id || pct[it.reply_id] != null
                return (
                  <div className="panel" key={it.reply_id}>
                    <CardHead it={it} cls={cls} extra={<span className="badge status-enrolled">tagged</span>} />
                    {d?.error ? (
                      <div className="banner warn">Draft error: {d.error}</div>
                    ) : d ? (
                      <>
                        <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>AI follow-up draft — edit before sending:</div>
                        <textarea value={edits[it.reply_id] ?? d.draft} disabled={sending}
                          onChange={(e) => setEdits((m) => ({ ...m, [it.reply_id]: e.target.value }))}
                          rows={5} style={{ width: '100%', boxSizing: 'border-box', fontSize: 13 }} />
                        {d.rationale && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>why: {d.rationale}</div>}
                      </>
                    ) : (
                      <div className="muted" style={{ fontSize: 12 }}>No draft yet — click <b>Draft follow-ups</b> above.</div>
                    )}

                    {pct[it.reply_id] != null && (
                      <div style={{ marginTop: 10 }}>
                        <ProgressBar pct={pct[it.reply_id]} />
                        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                          {pct[it.reply_id] >= 100 ? 'sent ✓' : 'sending the reply in the thread…'}
                        </div>
                      </div>
                    )}

                    {d && !d.error && pct[it.reply_id] == null && (
                      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 10 }}>
                        <button onClick={() => approve(it)} disabled={busy === it.reply_id} style={{ background: 'var(--green)' }}>
                          Approve follow-up →
                        </button>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {/* Everything else — collapsed */}
          {others.length > 0 && (
            <div style={{ marginTop: 28 }}>
              <button className="linklike" onClick={() => setShowOthers((v) => !v)} style={{ fontSize: 13 }}>
                {showOthers ? '▾' : '▸'} {num(others.length)} other replies (not interested / low confidence)
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
    </div>
  )
}
