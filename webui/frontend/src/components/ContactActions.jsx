import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner } from './ui.jsx'

// Working one contact on the call list.
//
// The whole design is the SPLIT. "Not right for this campaign" and "stop
// contacting this person" feel adjacent when you're working a list and are wildly
// different decisions — the first is a routing call, the second burns a contact
// for every campaign you will ever run. Collapsing them into one dismiss button is
// how good contacts quietly disappear.
//
// So the menu has two sections with different headings and different colour, and
// the second one states its blast radius before you use it.

const SNOOZE = [
  { days: 3, label: '3 days' }, { days: 7, label: 'a week' },
  { days: 30, label: 'a month' }, { days: 90, label: 'a quarter' },
]
const PAUSE = [
  { days: 30, label: '30 days' }, { days: 90, label: '90 days' },
  { days: 365, label: 'a year' },
]

export function EngagementBadge({ member }) {
  const st = member.engagement_state
  if (!st || st === 'active') return null
  const until = (member.paused_until || '').slice(0, 10)
  return (
    <span className={`badge engagement-${st}`}
      title={st === 'suppressed'
        ? `Do not contact${member.engagement_note ? ` — ${member.engagement_note}` : ''}`
        : `All outreach paused${until ? ` until ${until}` : ''}`}>
      {st === 'suppressed' ? 'do not contact' : `paused${until ? ` → ${until}` : ''}`}
    </span>
  )
}

export default function ContactActions({ member, onChanged }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(member.note || '')
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const away = (e) => { if (!ref.current?.contains(e.target)) setOpen(false) }
    const esc = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', esc)
    }
  }, [open])

  async function act(kind, fn) {
    setBusy(kind); setError(null)
    try { await fn(); onChanged?.(); setOpen(false) }
    catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  const member_ = { campaign_id: member.campaign_id, contact_id: member.contact_id }
  const m = (body) => api.updateMember({ ...member_, ...body })
  const eng = (body) => api.updateEngagement({ contact_id: member.contact_id, ...body })
  const state = member.engagement_state || 'active'
  const snoozed = member.snoozed_until && member.snoozed_until >= new Date()
    .toISOString().slice(0, 10)

  return (
    <span className="act-wrap" ref={ref}>
      <button type="button" className="ghost sm act-btn" aria-expanded={open}
        onClick={() => setOpen((v) => !v)} title="Work this contact">···</button>

      {open && (
        <div className="act-menu" role="dialog">
          <ErrorBanner error={error} />

          {/* ---- scoped to this campaign ---------------------------------- */}
          <div className="act-sec">
            In <b>{member.campaign_name || 'this campaign'}</b>
          </div>
          <div className="act-row">
            <button disabled={!!busy}
              onClick={() => act('worked', () => m({ action: 'worked', outcome: 'worked', note }))}>
              Mark worked
            </button>
            <button disabled={!!busy}
              onClick={() => act('noans', () => m({ action: 'worked', outcome: 'no_answer', note }))}>
              No answer
            </button>
          </div>
          <div className="act-row">
            <span className="act-lbl">Snooze</span>
            {SNOOZE.map((s) => (
              <button key={s.days} disabled={!!busy}
                onClick={() => act('snooze', () => m({ action: 'snooze', days: s.days }))}>
                {s.label}
              </button>
            ))}
          </div>
          {snoozed && (
            <div className="act-row">
              <button disabled={!!busy}
                onClick={() => act('unsnooze', () => m({ action: 'unsnooze' }))}>
                Un-snooze (hidden until {member.snoozed_until})
              </button>
            </div>
          )}
          <div className="act-row">
            <span className="act-lbl">Priority</span>
            <button disabled={!!busy}
              onClick={() => act('top', () => m({ action: 'priority', manual_priority: 100 }))}
              title="Pin to the top of the list. The computed score is kept.">
              Top of my list
            </button>
            <button disabled={!!busy}
              onClick={() => act('down', () => m({ action: 'priority', manual_priority: 5 }))}>
              Bottom
            </button>
            {member.manual_priority != null && (
              <button disabled={!!busy}
                onClick={() => act('reset', () => m({ action: 'priority', manual_priority: null }))}>
                Use the score again
              </button>
            )}
          </div>
          {member.state !== 'removed' ? (
            <div className="act-row">
              <button className="act-warn" disabled={!!busy}
                onClick={() => act('nofit', () => m({ action: 'worked', outcome: 'not_a_fit', note }))}
                title="Removes them from this campaign only. They stay contactable everywhere else.">
                Not a fit for this campaign
              </button>
            </div>
          ) : (
            <div className="act-row">
              <button disabled={!!busy}
                onClick={() => act('restore', () => m({ action: 'restore' }))}>
                Put back on this campaign
              </button>
            </div>
          )}

          <textarea rows={2} value={note} placeholder="Note on this contact…"
            onChange={(e) => setNote(e.target.value)}
            onBlur={() => note !== (member.note || '')
              && act('note', () => m({ action: 'note', note }))} />

          {/* ---- applies everywhere ---------------------------------------- */}
          <div className="act-sec danger">
            Everywhere — every campaign, now and future
          </div>
          {state === 'active' ? (
            <>
              <div className="act-row">
                <span className="act-lbl">Pause all</span>
                {PAUSE.map((s) => (
                  <button key={s.days} disabled={!!busy}
                    onClick={() => act('pause', () => eng({ engagement_state: 'paused', days: s.days, note }))}>
                    {s.label}
                  </button>
                ))}
              </div>
              <div className="act-row">
                <button className="act-danger" disabled={!!busy}
                  onClick={() => act('supp', () => eng({ engagement_state: 'suppressed', note }))}
                  title="Blocks enrollment everywhere. Enforced at the send gate, not just hidden here.">
                  Do not contact
                </button>
              </div>
            </>
          ) : (
            <div className="act-row">
              <button disabled={!!busy}
                onClick={() => act('resume', () => eng({ engagement_state: 'active' }))}>
                {busy === 'resume' ? <Spinner /> : 'Resume outreach'}
              </button>
              <span className="act-lbl">
                {state === 'suppressed' ? 'currently do-not-contact'
                  : `paused${member.paused_until ? ` until ${member.paused_until}` : ''}`}
              </span>
            </div>
          )}
          <div className="act-foot">
            Pausing and do-not-contact stop enrollment at the send gate — not just on
            this screen.
          </div>
        </div>
      )}
    </span>
  )
}
