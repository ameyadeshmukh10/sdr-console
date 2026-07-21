import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from '../components/ui.jsx'
import InboxList from '../components/replies/InboxList.jsx'
import ReplyDetail from '../components/replies/ReplyDetail.jsx'

const CAMPAIGNS = [
  { id: '', label: 'All campaigns' },
  { id: '10', label: '10 · sales-leadership' },
  { id: '11', label: '11 · revops' },
  { id: '12', label: '12 · partnerships' },
  { id: '13', label: '13 · sdr-bdr' },
]
const MIN_CONF = 0.50  // only interested/referral above this confidence surface for review

// Replies — an email-inbox view over both channels (Bison email + HeyReach
// LinkedIn). Left: the message list, sectioned by what needs doing. Right: the
// full conversation thread + the actions for that reply (tag / dismiss /
// reclassify / draft with the chosen reply agent / approve & send in-thread).
// HubSpot logging happens automatically in the background (hourly) — no button.
export default function RepliesPage() {
  const [queue, setQueue] = useState(null)
  const [drafts, setDrafts] = useState(null)
  const [agents, setAgents] = useState([{ id: 'standard', label: 'Standard Reply Agent', description: '' }])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)             // unified action feedback {err, text}
  const [scanning, setScanning] = useState(false)
  const [drafting, setDrafting] = useState(false)  // bulk draft
  const [busy, setBusy] = useState({})             // `${action}:${reply_id}` -> true
  const [lookback, setLookback] = useState(14)
  const [campaign, setCampaign] = useState('')
  const [channelFilter, setChannelFilter] = useState('all')
  const [selectedId, setSelectedId] = useState(null)
  const [collapsed, setCollapsed] = useState({ dismissed: true })  // section key -> collapsed
  const [edits, setEdits] = useState({})           // reply_id -> edited draft text
  const [pct, setPct] = useState({})               // reply_id -> send progress %
  const [sendErr, setSendErr] = useState({})       // reply_id -> inline send error
  const [playJobs, setPlayJobs] = useState({})     // reply_id -> playbook job status
  const [agentPick, setAgentPick] = useState({})   // reply_id -> agent id (optimistic)
  const [domainPrompt, setDomainPrompt] = useState({})  // reply_id -> needs a hand-typed domain
  const [domainInput, setDomainInput] = useState({})    // reply_id -> typed company domain
  const timers = useRef({})

  const loadQueue = () => api.repliesQueue().then(setQueue).catch((e) => setError(e.message))
  const loadDrafts = () => api.followupDrafts().then(setDrafts).catch(() => {})
  useEffect(() => {
    loadQueue(); loadDrafts()
    api.repliesAgents().then((r) => r.agents?.length && setAgents(r.agents)).catch(() => {})
  }, [])

  // Poll in-flight signal-playbook builds every 2.5s; refresh drafts when one
  // lands. Skips the setState when nothing changed (no pointless re-renders),
  // and a lost job (server restart) surfaces as an error instead of vanishing.
  useEffect(() => {
    const running = Object.entries(playJobs).filter(([, j]) => j.status === 'running')
    if (!running.length) return undefined
    const t = setInterval(async () => {
      const updates = await Promise.all(running.map(async ([rid, j]) => {
        try { return [rid, { ...(await api.playbookStatus(j.job_id)), job_id: j.job_id }] }
        catch {
          return [rid, { ...j, status: 'error', error: 'build job lost (server restarted?) — regenerate to retry' }]
        }
      }))
      setPlayJobs((m) => {
        let changed = false
        const next = { ...m }
        for (const [rid, s] of updates) {
          const prev = m[rid]
          if (!prev || prev.status !== s.status || prev.stage !== s.stage
              || prev.pct !== s.pct || (prev.log || []).length !== (s.log || []).length) {
            next[rid] = s
            changed = true
          }
          if (s.status !== 'running' && prev?.status === 'running') {
            // the finished build wrote a NEW draft — drop any stale manual edit
            // so Approve sends the fresh draft (with the play link), not old text
            setEdits((e) => { const n = { ...e }; delete n[rid]; return n })
            loadDrafts(); loadQueue()
          }
        }
        return changed ? next : m
      })
    }, 2500)
    return () => clearInterval(t)
  }, [playJobs])

  const mark = (action, id, v) => setBusy((b) => ({ ...b, [`${action}:${id}`]: v || undefined }))
  const busyFor = (id) => ({
    tag: busy[`tag:${id}`], dismiss: busy[`dismiss:${id}`], undismiss: busy[`undismiss:${id}`],
    reclassify: busy[`reclassify:${id}`], regen: busy[`regen:${id}`], approve: busy[`approve:${id}`],
    move: busy[`move:${id}`],
  })

  async function scan() {
    setScanning(true); setError(null); setMsg(null)
    try {
      const q = await api.scanReplies({ lookback_days: Number(lookback), campaign_id: campaign ? Number(campaign) : null })
      if (q.ok === false) setMsg({ err: true, text: 'Scan returned errors — see results.' })
      await Promise.all([loadQueue(), loadDrafts()])
    } catch (e) { setError(e.message) }
    finally { setScanning(false) }
  }

  async function act(action, item, call, okText) {
    const id = item.reply_id
    mark(action, id, true); setMsg(null)
    try {
      const r = await call()
      if (r.ok === false) setMsg({ err: true, text: r.error || `${action} failed` })
      else if (okText) setMsg({ err: false, text: okText })
      await loadQueue()
      return r
    } catch (e) { setMsg({ err: true, text: e.message }); return null }
    finally { mark(action, id, false) }
  }

  const tag = (it) => act('tag', it, () => api.tagReplies([it.reply_id]),
    `Tagged ${it.from_name || it.from_email} interested.`)
  const dismiss = (it) => act('dismiss', it, () => api.dismissReply(it.reply_id, 'handled_in_crm'),
    `${it.from_name || it.from_email} dismissed — they'll return if they reply again.`)
  const undismiss = (it) => act('undismiss', it, () => api.undismissReply(it.reply_id))
  const reclassify = async (it) => {
    const r = await act('reclassify', it, () => api.reclassifyReply(it.reply_id),
      `${it.from_name || it.from_email} reclassified as interested.`)
    if (r?.ok) setSelectedId(it.reply_id)
  }
  const move = async (it, to) => {
    const r = await act('move', it, () => api.moveReply(it.reply_id, to),
      to === 'interested'
        ? `${it.from_name || it.from_email} moved to Interested — draft when ready.`
        : `${it.from_name || it.from_email} parked in Follow up.`)
    if (r?.ok) setSelectedId(it.reply_id)
  }

  // Optimistic per-reply agent choice: the dropdown must win immediately — a
  // Draft click right after switching would otherwise send the stale agent
  // from the last queue snapshot.
  const agentFor = (it) => agentPick[it.reply_id] ?? it.agent ?? 'standard'

  async function setAgent(it, agent) {
    setAgentPick((m) => ({ ...m, [it.reply_id]: agent }))
    try {
      await api.setReplyAgent(it.reply_id, agent)
    } catch (e) { setMsg({ err: true, text: e.message }) }
  }

  async function regenerate(it, companyDomain) {
    const id = it.reply_id
    // Reuse a domain the user already typed this session so the normal
    // Regenerate button doesn't dead-end on the same lead a second time.
    const domain = (companyDomain ?? domainInput[id]) || undefined
    mark('regen', id, true); setMsg(null)
    try {
      const r = await api.regenerateDraft(id, agentFor(it), domain)
      if (r.ok === false) {
        // The signal-playbook agent couldn't resolve the account — prompt for a
        // hand-typed domain inline instead of surfacing a dead-end banner.
        if (r.need_domain) setDomainPrompt((m) => ({ ...m, [id]: true }))
        else setMsg({ err: true, text: r.error || 'draft failed' })
      } else {
        setDomainPrompt((m) => { const n = { ...m }; delete n[id]; return n })
        if (r.async) setPlayJobs((m) => ({ ...m, [id]: { job_id: r.job_id, status: 'running', stage: 'research', pct: 2 } }))
        else {
          setEdits((m) => { const n = { ...m }; delete n[id]; return n })
          await loadDrafts()
        }
      }
    } catch (e) { setMsg({ err: true, text: e.message }) }
    finally { mark('regen', id, false) }
  }

  const submitDomain = (it) => {
    const d = (domainInput[it.reply_id] || '').trim()
    if (d) regenerate(it, d)
  }

  async function bulkDraft() {
    setDrafting(true); setMsg(null)
    try { await api.draftFollowups(); await loadDrafts() }
    catch (e) { setMsg({ err: true, text: e.message }) }
    finally { setDrafting(false) }
  }

  const clearPct = (id) => setPct((p) => { const n = { ...p }; delete n[id]; return n })

  async function approve(it) {
    const id = it.reply_id
    mark('approve', id, true); setMsg(null)
    setSendErr((e) => { const n = { ...e }; delete n[id]; return n })
    setPct((p) => ({ ...p, [id]: 8 }))
    timers.current[id] = setInterval(
      () => setPct((p) => ({ ...p, [id]: Math.min((p[id] || 8) + 11, 90) })), 180)
    const stop = () => { clearInterval(timers.current[id]); delete timers.current[id] }
    const fail = (text) => { clearPct(id); setSendErr((e) => ({ ...e, [id]: text })); setMsg({ err: true, text }) }
    try {
      const r = await api.approveFollowup(id, edits[id] ?? draftFor(it)?.draft)
      stop()
      if (r.ok === false) { fail(r.error || 'send failed') }
      else {
        setPct((p) => ({ ...p, [id]: 100 }))
        const via = it.channel === 'linkedin' ? 'via LinkedIn' : 'in the thread'
        setMsg({ err: false, text: `Sent follow-up to ${it.from_name || it.from_email} ${via}.` })
        setTimeout(() => { Promise.all([loadQueue(), loadDrafts()]); clearPct(id) }, 850)
      }
    } catch (e) { stop(); fail(e.message) }
    finally { mark('approve', id, false) }
  }

  async function previewPlay(slug) {
    try { window.open(await api.playHtmlBlobUrl(slug), '_blank', 'noopener') }
    catch (e) { setMsg({ err: true, text: e.message }) }
  }

  // ---- Partition into inbox sections --------------------------------------
  const items = queue?.items || []
  const dismissedItems = queue?.dismissed || []
  const draftBy = useMemo(
    () => Object.fromEntries((drafts?.items || []).map((d) => [String(d.reply_id), d])), [drafts])
  const draftFor = (it) => draftBy[String(it.reply_id)]

  const inChannel = (it) => channelFilter === 'all' || (it.channel || 'email') === channelFilter
  const conf = (it) => it.classifier?.confidence || 0
  const isInterested = (it) => !!it.classifier?.interested && (conf(it) > MIN_CONF || !!it.reclassified)

  const visible = items.filter(inChannel)
  // Follow up = we replied (or parked) and are waiting on the lead. A reply that
  // arrives after our follow-up comes back flagged post_followup and routes to
  // Possible for review, whatever the classifier said.
  const followup = visible.filter((it) => it.handled)
  const possible = visible.filter((it) => !it.handled
    && (it.post_followup || (isInterested(it) && !it.already_interested)))
  const interested = visible.filter((it) => !it.handled && !it.post_followup
    && isInterested(it) && it.already_interested)
  const others = visible.filter((it) => !it.handled && !it.post_followup && !isInterested(it))
  const dismissedList = dismissedItems.filter(inChannel)
  const liCount = items.filter((it) => it.channel === 'linkedin').length
  const needDrafts = interested.filter((it) => !draftFor(it)).length

  const sectionOf = (it) => {
    if (!it) return null
    if (it.dismissed) return 'dismissed'
    if (it.handled) return 'followup'
    if (it.post_followup) return 'possible'
    if (!isInterested(it)) return 'other'
    return it.already_interested ? 'interested' : 'possible'
  }

  const all = [...interested, ...possible, ...followup, ...others, ...dismissedList]
  const selected = all.find((it) => String(it.reply_id) === String(selectedId)) || null
  useEffect(() => {   // keep something sensible selected as the queue changes
    if (!selected && all.length) setSelectedId(all[0].reply_id)
  }, [queue, drafts, channelFilter])  // eslint-disable-line react-hooks/exhaustive-deps

  const autosync = queue?.hubspot_autosync
  const sections = [
    {
      key: 'interested', label: 'Interested — draft & send', items: interested,
      empty: 'Tag a possible-interested reply to start a follow-up.',
      action: needDrafts > 0 && (
        <button className="linklike" style={{ fontSize: 11, textTransform: 'none', letterSpacing: 0 }}
          onClick={bulkDraft} disabled={drafting}>
          {drafting ? 'drafting…' : `✎ draft all (${needDrafts})`}
        </button>
      ),
    },
    { key: 'possible', label: 'Possible interested — review', items: possible, empty: 'Nothing to review — scan the inbox.' },
    {
      key: 'followup', label: 'Follow up', items: followup,
      empty: 'Approve & send a follow-up and it parks here until the lead replies.',
    },
    { key: 'other', label: 'Other — not interested / low confidence', items: others, empty: 'Nothing filtered out.' },
    { key: 'dismissed', label: 'Dismissed', items: dismissedList, dim: true, hideWhenEmpty: true },
  ]

  return (
    <div>
      <h1 className="page-title">Replies</h1>
      <p className="page-sub">
        Every reply from the email and LinkedIn inboxes, classified by Claude.
        Review, tag, dismiss what you've handled in the CRM, and send AI-drafted follow-ups in the
        prospect's own thread. Replies and follow-ups log to HubSpot automatically every hour.
      </p>

      <div className="panel" style={{ marginBottom: 16 }}>
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
          {liCount > 0 && (
            <span className="row" style={{ gap: 6 }}>
              {[['all', 'All'], ['email', '✉ Email'], ['linkedin', 'in LinkedIn']].map(([v, label]) => (
                <button key={v} onClick={() => setChannelFilter(v)}
                  className={channelFilter === v ? '' : 'linklike'}
                  style={channelFilter === v
                    ? { fontSize: 12, padding: '3px 12px' }
                    : { fontSize: 12, padding: '3px 12px', border: '1px solid var(--border)', background: 'transparent', borderRadius: 999 }}>
                  {label}
                </button>
              ))}
            </span>
          )}
          <span className="grow" />
          <span className="muted" style={{ alignSelf: 'center', fontSize: 12.5 }}>
            {queue?.scanned_at ? <>Last scanned {new Date(queue.scanned_at).toLocaleString()}</> : 'Not scanned yet'}
            {autosync?.ok === false && (
              <span title={`Last HubSpot auto-log failed (${autosync.at || ''}): ${autosync.summary || ''}`}
                style={{ color: 'var(--red)', marginLeft: 8, cursor: 'help' }}>● HubSpot sync issue</span>
            )}
          </span>
        </div>
      </div>

      <ErrorBanner error={error} />
      {msg && <div className={`banner ${msg.err ? 'warn' : 'info'}`}>{msg.text}</div>}

      {!queue ? <Spinner label="Loading…" /> : !queue.available ? (
        <div className="empty">No replies scanned yet — set a lookback and <b>Scan inbox</b>.</div>
      ) : (
        <div className="inbox">
          <InboxList sections={sections} selectedId={selectedId} onSelect={(it) => setSelectedId(it.reply_id)}
            collapsed={collapsed}
            onToggle={(k) => setCollapsed((c) => ({ ...c, [k]: !c[k] }))} />
          <ReplyDetail
            item={selected}
            section={sectionOf(selected)}
            agents={agents}
            agentSel={selected ? agentFor(selected) : 'standard'}
            draft={selected ? draftFor(selected) : null}
            editValue={selected ? edits[selected.reply_id] : undefined}
            setEdit={(id, v) => setEdits((m) => ({ ...m, [id]: v }))}
            busy={selected ? busyFor(selected.reply_id) : {}}
            playJob={selected ? playJobs[selected.reply_id] : null}
            sendPct={selected ? pct[selected.reply_id] : null}
            sendErr={selected ? sendErr[selected.reply_id] : null}
            needDomain={selected ? !!domainPrompt[selected.reply_id] : false}
            domainValue={selected ? domainInput[selected.reply_id] : ''}
            onDomainChange={(id, v) => setDomainInput((m) => ({ ...m, [id]: v }))}
            onSubmitDomain={submitDomain}
            onTag={tag} onDismiss={dismiss} onUndismiss={undismiss} onReclassify={reclassify}
            onMove={move} onAgentChange={setAgent} onRegenerate={regenerate} onApprove={approve}
            onPreviewPlay={previewPlay}
          />
        </div>
      )}

      {queue?.counts?.unsubscribed > 0 && (
        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          {num(queue.counts.unsubscribed)} opt-out{queue.counts.unsubscribed === 1 ? '' : 's'} auto-unsubscribed
          and suppressed in the email platform this scan.
        </div>
      )}
    </div>
  )
}
