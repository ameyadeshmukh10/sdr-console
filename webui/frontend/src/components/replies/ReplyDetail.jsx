import { Spinner } from '../ui.jsx'
import ThreadView from './ThreadView.jsx'
import { INTENT_COLOR } from './InboxList.jsx'

const PLAY_STAGES = ['research', 'deck-data', 'render', 'publish', 'draft']

function ProgressBar({ pct }) {
  return (
    <div style={{ height: 8, background: 'var(--border)', borderRadius: 4, overflow: 'hidden' }}>
      <div style={{ height: '100%', width: `${pct}%`, background: 'var(--accent, #0a7)', transition: 'width .25s ease' }} />
    </div>
  )
}

function PlayProgress({ job }) {
  const stages = job.stages || PLAY_STAGES   // backend list is authoritative
  const doneIdx = job.stage === 'done' ? stages.length : stages.indexOf(job.stage)
  return (
    <div style={{ marginTop: 12 }}>
      <div className="muted" style={{ fontSize: 12, fontWeight: 600 }}>
        Building the signal play — research, deck, live page publish, then the draft. This takes a few minutes.
      </div>
      <div className="stage-list">
        {stages.map((s, i) => (
          <span key={s} className={`stage-chip${i < doneIdx ? ' done' : i === doneIdx ? ' now' : ''}`}>
            {i < doneIdx ? '✓ ' : ''}{s}
          </span>
        ))}
      </div>
      <ProgressBar pct={job.pct || 2} />
      {job.log?.length > 0 && (
        <div className="muted" style={{ fontSize: 11, marginTop: 5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {job.log[job.log.length - 1]}
        </div>
      )}
    </div>
  )
}

// The play chip shown once a signal-playbook draft exists: the LIVE published
// page, the local HTML preview, and a warning when the page URL isn't on
// everworker.ai (wrong portal domain / missing content scope).
function PlayChip({ play, onPreview }) {
  if (!play) return null
  return (
    <div style={{ marginTop: 8 }}>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <span className="badge status-generated">signal play</span>
        {play.page_url && (
          <a href={play.page_url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
            View live play ↗
          </a>
        )}
        {play.slug && (
          <button className="linklike" style={{ fontSize: 12 }} onClick={() => onPreview(play.slug)}>
            Local preview ↗
          </button>
        )}
      </div>
      {play.page_url && play.url_domain_ok === false && (
        <div className="banner warn" style={{ marginTop: 8, marginBottom: 0, fontSize: 12.5 }}>
          The live page is not on <b>everworker.ai</b> — check the portal's primary website
          domain (or set SIGNAL_PLAY_DOMAIN) and that the HubSpot token has the <b>content</b> scope.
        </div>
      )}
    </div>
  )
}

export default function ReplyDetail({
  item, section, agents, agentSel, draft, editValue, setEdit,
  busy, playJob, sendPct, sendErr, needDomain, domainValue, onDomainChange, onSubmitDomain,
  onTag, onDismiss, onUndismiss, onReclassify, onMove, onAgentChange, onRegenerate, onApprove, onPreviewPlay,
}) {
  if (!item) {
    return <div className="inbox-detail"><div className="empty" style={{ border: 'none', background: 'none' }}>
      Select a reply on the left to read the conversation.
    </div></div>
  }
  const cls = item.classifier || {}
  const li = item.channel === 'linkedin'
  const name = item.from_name || `${item.first_name || ''} ${item.last_name || ''}`.trim() || item.from_email || 'LinkedIn lead'
  const sending = sendPct != null
  const building = playJob && playJob.status === 'running'
  const playMeta = draft?.play || (playJob?.status === 'done' ? playJob.play : null)

  const Dismiss = ({ label = 'Dismiss — handled in CRM' }) => (
    <button className="ghost" onClick={() => onDismiss(item)} disabled={!!busy.dismiss}
      title="Clears this lead from the queue (e.g. you already replied or booked them from the CRM). They come back automatically if they send a new reply.">
      {busy.dismiss ? <Spinner label="Dismissing…" /> : label}
    </button>
  )

  return (
    <div className="inbox-detail">
      <div className="detail-head">
        <div className="row between" style={{ alignItems: 'flex-start' }}>
          <div>
            <div style={{ fontSize: 18, fontWeight: 700 }}>{name}</div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 2 }}>
              {li
                ? (item.profile_url && <a href={item.profile_url} target="_blank" rel="noreferrer">LinkedIn profile ↗</a>)
                : (item.from_email || item.lead_email)}
              {(item.title || item.company) && <> · {[item.title, item.company].filter(Boolean).join(' · ')}</>}
            </div>
            {item.sending_email && (
              <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                our sender: {item.sending_email}{li ? ' · LinkedIn' : ''}
              </div>
            )}
          </div>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            <span className={`badge channel-${li ? 'linkedin' : 'email'}`}>{li ? 'in LinkedIn' : '✉ Email'}</span>
            <span className="badge" style={{ color: INTENT_COLOR[cls.intent] || 'var(--muted)' }}>
              {cls.intent}{cls.confidence != null ? ` · ${Math.round(cls.confidence * 100)}%` : ''}
            </span>
            {item.already_interested && <span className="badge status-enrolled">interested</span>}
            {item.handled && <span className="badge status-generated">{item.parked ? 'parked' : 'follow-up sent'}</span>}
          </div>
        </div>
        {cls.reason && (
          <div className="reason-callout">
            <b>Why the classifier put it here:</b> {cls.reason}
            {item.classifier_original && item.reclassified && (
              <span className="muted"> (model originally said: {item.classifier_original.reason || item.classifier_original.intent})</span>
            )}
          </div>
        )}
        {item.dismissed && (
          <div className="banner info" style={{ marginTop: 10, marginBottom: 0 }}>
            Dismissed {item.dismissed.at ? `on ${new Date(item.dismissed.at).toLocaleDateString()}` : ''} — a new
            reply from this lead resurfaces them automatically.{' '}
            <button className="linklike" onClick={() => onUndismiss(item)} disabled={!!busy.undismiss}>
              {busy.undismiss ? 'Restoring…' : 'Restore to queue'}
            </button>
            {' · '}
            <button className="linklike" onClick={() => onMove(item, 'followup')} disabled={!!busy.move}
              title="Park this lead in Follow up — you're on it or handled it outside the console.">
              {busy.move ? 'Moving…' : 'Move to Follow up'}
            </button>
          </div>
        )}
      </div>

      <ThreadView item={item} />

      {/* --- Actions, by section ------------------------------------------- */}
      {section === 'possible' && !item.dismissed && (
        <div className="row" style={{ justifyContent: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
          <Dismiss />
          <button onClick={() => onTag(item)} disabled={!!busy.tag} style={{ background: 'var(--green)', color: '#fff' }}>
            {busy.tag ? <Spinner label="Tagging…" /> : 'Tag interested →'}
          </button>
        </div>
      )}

      {section === 'other' && !item.dismissed && (
        <div className="row" style={{ justifyContent: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
          <Dismiss label="Dismiss" />
          <button onClick={() => onReclassify(item)} disabled={!!busy.reclassify}
            title="The classifier got it wrong — mark this reply interested. It moves to the Interested queue and is tagged in the email platform.">
            {busy.reclassify ? <Spinner label="Reclassifying…" /> : '↺ Reclassify as interested'}
          </button>
        </div>
      )}

      {section === 'followup' && (
        <div className="row" style={{ justifyContent: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
          <Dismiss label="Dismiss" />
          <button onClick={() => onMove(item, 'interested')} disabled={!!busy.move}
            title="Un-park this conversation and move it back to Interested so you can draft and send again.">
            {busy.move ? <Spinner label="Moving…" /> : '↩ Move to Interested'}
          </button>
        </div>
      )}

      {section === 'interested' && !item.dismissed && !item.handled && (
        <div>
          <div className="row" style={{ gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <label className="field" style={{ minWidth: 240 }}>Reply agent
              <select value={agentSel} disabled={building || sending}
                onChange={(e) => onAgentChange(item, e.target.value)}>
                {agents.map((a) => <option key={a.id} value={a.id} title={a.description}>{a.label}</option>)}
              </select>
            </label>
            <button className="ghost" onClick={() => onRegenerate(item)} disabled={!!busy.regen || building || sending}
              title="Re-draft the follow-up with the selected agent">
              {busy.regen ? <Spinner label="Drafting…" /> : draft ? '↻ Regenerate draft' : '✎ Draft follow-up'}
            </button>
            <span style={{ flex: 1 }} />
            <Dismiss />
          </div>
          {agents.find((a) => a.id === agentSel)?.description && (
            <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
              {agents.find((a) => a.id === agentSel).description}
            </div>
          )}

          {needDomain && !building && (
            <div className="banner warn" style={{ marginTop: 10 }}>
              <div style={{ fontSize: 12.5, marginBottom: 8 }}>
                We couldn't work out {item.company ? <b>{item.company}</b> : 'this lead'}&apos;s company website
                {li && !(item.from_email || item.lead_email) ? ' (LinkedIn leads often have no email to derive it from)' : ''} —
                the Signal Playbook agent researches the company, so it needs the domain. Enter it and we&apos;ll build the play.
              </div>
              <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                <input value={domainValue || ''} placeholder="e.g. ibm.com"
                  onChange={(e) => onDomainChange(item.reply_id, e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') onSubmitDomain(item) }}
                  disabled={!!busy.regen}
                  style={{ flex: 1, minWidth: 180, boxSizing: 'border-box' }} />
                <button onClick={() => onSubmitDomain(item)} disabled={!!busy.regen || !(domainValue || '').trim()}>
                  {busy.regen ? <Spinner label="Building…" /> : 'Build play with this domain'}
                </button>
              </div>
            </div>
          )}
          {building && <PlayProgress job={playJob} />}
          {playJob?.status === 'error' && (
            <div className="banner warn" style={{ marginTop: 10 }}>Signal play build failed: {playJob.error}</div>
          )}
          {draft?.fallback && (
            <div className="banner warn" style={{ marginTop: 10, fontSize: 12.5 }}>
              The play build hit an error ({draft.error || 'see logs'}) — this is a standard draft without the link.
            </div>
          )}

          {draft?.error && !draft?.draft ? (
            <div className="banner warn" style={{ marginTop: 12 }}>Draft error: {draft.error}</div>
          ) : draft && (
            <div style={{ marginTop: 14 }}>
              <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>
                AI follow-up draft ({draft.agent === 'signal-playbook' ? 'Signal Playbook' : 'Standard'} agent) — edit before sending:
              </div>
              <textarea value={editValue ?? draft.draft} disabled={sending || building}
                onChange={(e) => setEdit(item.reply_id, e.target.value)}
                rows={8} style={{ width: '100%', boxSizing: 'border-box', fontSize: 13 }} />
              {draft.rationale && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>why: {draft.rationale}</div>}
              <PlayChip play={playMeta} onPreview={onPreviewPlay} />
            </div>
          )}
          {!draft && !building && (
            <div className="muted" style={{ fontSize: 12.5, marginTop: 12 }}>
              No draft yet — pick an agent and click <b>Draft follow-up</b>.
            </div>
          )}

          {sendPct != null && (
            <div style={{ marginTop: 10 }}>
              <ProgressBar pct={sendPct} />
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                {sendPct >= 100 ? 'sent ✓' : li ? 'sending the LinkedIn reply…' : 'sending the reply in the thread…'}
              </div>
            </div>
          )}
          {sendErr && <div className="banner warn" style={{ marginTop: 8 }}>Send failed: {sendErr}</div>}

          {draft && !building && sendPct == null && (
            <div className="row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
              <button onClick={() => onApprove(item)} disabled={!!busy.approve} style={{ background: 'var(--green)', color: '#fff' }}>
                {sendErr ? 'Retry send →' : 'Approve & send →'}
              </button>
            </div>
          )}
        </div>
      )}

      {section === 'followup' && (
        <div className="muted" style={{ fontSize: 12.5 }}>
          {item.parked
            ? <>Parked in Follow up — nothing was sent from the console. A new reply from
              {' '}{name} moves this conversation to <b>Possible interested</b> for review.</>
            : <>Follow-up sent — it appears in the thread above. When {name} replies, this
              conversation moves to <b>Possible interested</b> for review automatically.</>}
        </div>
      )}
    </div>
  )
}
