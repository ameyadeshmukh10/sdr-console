import { useEffect, useState } from 'react'
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

// Pillar — Interested-reply detection. Scan the Bison inbox, classify each reply
// with Claude, review the flagged ones, then (gated) write mark-as-interested +
// the Interested tag back to Bison.
export default function RepliesPage() {
  const [queue, setQueue] = useState(null)
  const [error, setError] = useState(null)
  const [scanning, setScanning] = useState(false)
  const [lookback, setLookback] = useState(14)
  const [campaign, setCampaign] = useState('')
  const [selected, setSelected] = useState(new Set())
  const [confirming, setConfirming] = useState(false)
  const [ack, setAck] = useState(false)
  const [tagging, setTagging] = useState(false)
  const [result, setResult] = useState(null)
  const [expanded, setExpanded] = useState(null)
  const [samplesBusy, setSamplesBusy] = useState(null)
  const [samples, setSamples] = useState(null)
  const [drafts, setDrafts] = useState(null)
  const [drafting, setDrafting] = useState(false)
  const [edits, setEdits] = useState({})       // reply_id -> edited draft text
  const [sending, setSending] = useState(null)  // reply_id being approved
  const [fuMsg, setFuMsg] = useState(null)     // inline follow-up action feedback

  function loadDrafts() {
    api.followupDrafts().then(setDrafts).catch(() => {})
  }
  useEffect(() => { loadDrafts() }, [])

  async function generateDrafts() {
    setDrafting(true); setError(null)
    try { await api.draftFollowups(); loadDrafts() }
    catch (e) { setError(e.message) }
    finally { setDrafting(false) }
  }

  async function approveFollowup(it) {
    setSending(it.reply_id); setError(null); setFuMsg(null)
    try {
      const msg = edits[it.reply_id] ?? it.draft
      const r = await api.approveFollowup(it.reply_id, msg)
      if (r.ok === false) setFuMsg({ err: true, text: r.error || 'send failed' })
      else setFuMsg({ err: false, text: `Sent follow-up to ${it.from_name || it.from_email} and pushed to the Interested Follow-up campaign.` })
      loadDrafts()
    } catch (e) { setFuMsg({ err: true, text: e.message }) }
    finally { setSending(null) }
  }

  async function draftSamples(it) {
    setSamplesBusy(it.reply_id); setSamples(null)
    try {
      const r = await api.samples({ from_email: it.from_email })
      setSamples({ ...r, from_name: it.from_name })
    } catch (e) { setError(e.message) }
    finally { setSamplesBusy(null) }
  }

  function loadQueue() {
    api.repliesQueue().then((q) => {
      setQueue(q)
      // preselect flagged-interested + not already tagged
      const pre = new Set((q.items || [])
        .filter((it) => it.classifier?.interested && !it.already_interested)
        .map((it) => it.reply_id))
      setSelected(pre)
    }).catch((e) => setError(e.message))
  }
  useEffect(() => { loadQueue() }, [])

  async function scan() {
    setScanning(true); setError(null); setResult(null)
    try {
      const q = await api.scanReplies({ lookback_days: Number(lookback), campaign_id: campaign ? Number(campaign) : null })
      if (q.ok === false) setError('Scan returned errors — see results.')
      loadQueue()
    } catch (e) { setError(e.message) }
    finally { setScanning(false) }
  }

  function toggle(id) {
    setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  }

  async function tag() {
    setTagging(true); setError(null)
    try {
      const r = await api.tagReplies([...selected])
      setResult(r); setConfirming(false); setAck(false)
      if (!r.ok) setError(`${r.failed} reply(ies) failed to tag.`)
      loadQueue()
    } catch (e) { setError(e.message) }
    finally { setTagging(false) }
  }

  const items = queue?.items || []
  const counts = queue?.counts || {}

  return (
    <div>
      <h1 className="page-title">Replies — interested detection</h1>
      <p className="page-sub">Scan the Bison inbox, classify with Claude, then approve to apply the Interested tag. Auto-replies, non-lead senders, and connection-test mail are filtered out; explicit unsubscribe / "close the file" replies are suppressed.</p>

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

      {result && (
        <div className="banner info">Tagged <b>{num(result.tagged)}</b> · failed <b>{num(result.failed)}</b> in Bison (mark-as-interested + Interested tag).</div>
      )}

      {queue && (
        <div className="grid stat-grid" style={{ marginBottom: 18 }}>
          <Stat label="Scanned" value={num(counts.scanned || 0)} sub={`last ${queue.lookback_days || 14}d`} />
          <Stat label="Flagged interested" value={num(counts.flagged || 0)} sub="not yet tagged" />
          <Stat label="Filtered out" value={num(counts.filtered || 0)} sub="auto-reply / non-lead / test" />
          <Stat label={queue.unsubscribed?.applied ? 'Unsubscribed' : 'To unsubscribe'}
            value={num(counts.unsubscribed || 0)}
            sub={queue.unsubscribed?.applied ? 'suppressed in Bison' : 'close-file / opt-out (preview)'} />
        </div>
      )}

      {!queue ? <Spinner label="Loading…" /> : items.length === 0 ? (
        <div className="empty">No classified replies yet. Set a lookback window and click <b>Scan inbox</b>.</div>
      ) : (
        <>
          <div className="row between" style={{ marginBottom: 10 }}>
            <span className="muted">{num(items.length)} replies</span>
            <button onClick={() => setConfirming(true)} disabled={selected.size === 0}>
              Tag {num(selected.size)} as interested →
            </button>
          </div>
          <div className="panel" style={{ padding: 0 }}>
            <table>
              <thead><tr><th></th><th>From</th><th>Reply</th><th>Verdict</th><th>Reason</th><th>Conf.</th><th></th></tr></thead>
              <tbody>
                {items.map((it) => {
                  const cls = it.classifier || {}
                  return (
                    <tr key={it.reply_id}>
                      <td><input type="checkbox" checked={selected.has(it.reply_id)} onChange={() => toggle(it.reply_id)} style={{ width: 'auto' }} /></td>
                      <td>
                        <div>{it.from_name || '—'}</div>
                        <div className="muted" style={{ fontSize: 11 }}>{it.from_email}</div>
                        {it.already_interested && <span className="badge status-enrolled" style={{ marginTop: 4 }}>already tagged</span>}
                      </td>
                      <td style={{ maxWidth: 320 }}>
                        <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>{it.subject}</div>
                        <div style={{ cursor: 'pointer', whiteSpace: expanded === it.reply_id ? 'pre-wrap' : 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 320 }}
                          onClick={() => setExpanded(expanded === it.reply_id ? null : it.reply_id)}>
                          {it.text_body}
                        </div>
                      </td>
                      <td><span className="badge" style={{ color: cls.interested ? 'var(--green)' : 'var(--muted)', borderColor: cls.interested ? 'var(--green)' : 'var(--border)' }}>
                        {cls.interested ? 'interested' : 'no'}</span>
                        <div><span className="badge" style={{ marginTop: 4, color: INTENT_COLOR[cls.intent] || 'var(--muted)', borderColor: 'var(--border)' }}>{cls.intent}</span></div>
                      </td>
                      <td className="muted" style={{ maxWidth: 260, fontSize: 12 }}>{cls.reason}</td>
                      <td>{cls.confidence != null ? `${Math.round(cls.confidence * 100)}%` : '—'}</td>
                      <td>
                        <button className="ghost" style={{ padding: '5px 10px', fontSize: 12.5 }}
                          onClick={() => draftSamples(it)} disabled={samplesBusy === it.reply_id}
                          title="Draft 3 sample emails our AI would write for this lead's company (show-arm fulfillment)">
                          {samplesBusy === it.reply_id ? <Spinner /> : 'Draft 3 samples'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* Interested follow-ups: draft → approve (push to campaign + auto-send) */}
      <div className="row between" style={{ marginTop: 30, marginBottom: 8 }}>
        <h2 className="section-h" style={{ margin: 0 }}>Interested follow-ups</h2>
        <button className="ghost" onClick={generateDrafts} disabled={drafting}>
          {drafting ? <Spinner label="Drafting…" /> : '✎ Draft follow-ups'}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
        AI-drafted reply per interested lead. Approve to push the lead into the <b>Interested Follow-up</b> campaign and send the (edited) reply in the thread.
      </p>
      {fuMsg && <div className={`banner ${fuMsg.err ? 'warn' : 'info'}`} style={{ marginBottom: 10 }}>{fuMsg.text}</div>}
      {!drafts?.items?.length ? (
        <div className="empty">No follow-up drafts yet. Click <b>Draft follow-ups</b> to generate them for the interested replies above.</div>
      ) : (
        <div className="grid" style={{ gap: 12 }}>
          {drafts.items.map((it) => {
            const sent = it.status === 'sent'
            return (
              <div className="panel" key={it.reply_id} style={{ opacity: sent ? 0.7 : 1 }}>
                <div className="row between" style={{ marginBottom: 6 }}>
                  <span><b>{it.from_name || it.from_email}</b> <span className="muted" style={{ fontSize: 12 }}>{it.from_email}</span></span>
                  <span className="row" style={{ gap: 8 }}>
                    <span className="badge" style={{ color: INTENT_COLOR[it.intent] || 'var(--muted)' }}>{it.intent}</span>
                    {sent && <span className="badge" style={{ color: 'var(--green)', borderColor: 'var(--green)' }}>sent ✓</span>}
                  </span>
                </div>
                {it.original_reply && <div className="muted" style={{ fontSize: 12, marginBottom: 8, borderLeft: '2px solid var(--border)', paddingLeft: 8 }}>{it.original_reply.slice(0, 220)}</div>}
                {it.error ? <div className="banner warn">Draft error: {it.error}</div> : (
                  <textarea value={edits[it.reply_id] ?? it.draft} disabled={sent}
                    onChange={(e) => setEdits((m) => ({ ...m, [it.reply_id]: e.target.value }))}
                    rows={5} style={{ width: '100%', boxSizing: 'border-box', fontSize: 13 }} />
                )}
                {it.rationale && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>why: {it.rationale}</div>}
                {!sent && !it.error && (
                  <div className="row" style={{ justifyContent: 'flex-end', marginTop: 8 }}>
                    <button onClick={() => approveFollowup(it)} disabled={sending === it.reply_id}
                      style={{ background: 'var(--green)' }}>
                      {sending === it.reply_id ? <Spinner label="Sending…" /> : 'Approve & send →'}
                    </button>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {confirming && (
        <>
          <div className="drawer-backdrop" onClick={() => !tagging && setConfirming(false)} />
          <div className="panel" style={{ position: 'fixed', top: '30%', left: '50%', transform: 'translateX(-50%)', zIndex: 50, width: 460, maxWidth: '92vw' }}>
            <h2 style={{ marginTop: 0 }}>Confirm interested tagging</h2>
            <p className="muted">
              This marks <b>{num(selected.size)}</b> repl{selected.size === 1 ? 'y' : 'ies'} as interested and applies the
              Interested tag to each lead in Bison. Outward action, cannot be undone.
            </p>
            <label className="row" style={{ gap: 8, margin: '14px 0' }}>
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} style={{ width: 'auto' }} />
              <span>I understand this writes to Bison.</span>
            </label>
            <div className="row" style={{ gap: 10, justifyContent: 'flex-end' }}>
              <button className="ghost" onClick={() => setConfirming(false)} disabled={tagging}>Cancel</button>
              <button onClick={tag} disabled={!ack || tagging} style={{ background: 'var(--green)' }}>
                {tagging ? <Spinner label="Tagging…" /> : 'Tag in Bison now'}
              </button>
            </div>
          </div>
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
