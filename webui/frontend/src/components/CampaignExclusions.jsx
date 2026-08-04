import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import ContactLink from './ContactLink.jsx'

// Who is OUT of this campaign, and how to put them back.
//
// The case this exists for: you call someone, they say never contact us again, you
// switch them off. A year later there is a new product and that answer may not hold
// any more. If the exclusion is invisible, it is permanent by accident — so the
// list has to be findable, explain itself, and be one click to undo.
//
// Two kinds, kept apart because undoing them is a different decision:
//   LOCAL   dropped from this campaign only. Restoring affects nothing else.
//   GLOBAL  outreach off for the person everywhere. They still MATCH this campaign
//           (they appear in its target count) — they are just never added.
export default function CampaignExclusions({ campaignId, onChanged }) {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  function load() {
    api.campaignExcluded(campaignId)
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [campaignId])

  async function act(key, fn) {
    setBusy(key); setError(null)
    try { await fn(); load(); onChanged?.() }
    catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  if (!data) return null
  const local = data.local || []
  const global_ = data.global || []
  if (!local.length && !global_.length) return null

  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <div className="card-h">
        <div>
          <h3>Excluded from this campaign</h3>
          <div className="card-note">
            Nothing here is permanent. The reason someone was excluded may not hold a
            year later — this is where you check and change your mind.
          </div>
        </div>
        <div className="card-meta">{num(local.length + global_.length)} people</div>
      </div>

      <ErrorBanner error={error} />

      {local.length > 0 && (
        <>
          <div className="excl-h">Dropped from this campaign only</div>
          <ul className="excl-list">
            {local.map((c) => (
              <li key={c.contact_id}>
                <ContactLink contact={{ contact_id: c.contact_id,
                  first_name: (c.name || '').split(' ')[0],
                  last_name: (c.name || '').split(' ').slice(1).join(' ') }} />
                <span className="muted">{c.title || ''}{c.company ? ` · ${c.company}` : ''}</span>
                {c.outcome && <span className="badge">{c.outcome.replace(/_/g, ' ')}</span>}
                {c.note && <span className="muted excl-note">“{c.note}”</span>}
                <button className="ghost sm" disabled={!!busy}
                  onClick={() => act(c.contact_id, () => api.updateMember({
                    campaign_id: campaignId, contact_id: c.contact_id, action: 'restore' }))}>
                  {busy === c.contact_id ? <Spinner /> : 'Put back'}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {global_.length > 0 && (
        <>
          <div className="excl-h danger">
            Outreach switched off everywhere — they still match this campaign
          </div>
          <ul className="excl-list">
            {global_.map((c) => (
              <li key={c.contact_id}>
                <ContactLink contact={{ contact_id: c.contact_id,
                  first_name: (c.name || '').split(' ')[0],
                  last_name: (c.name || '').split(' ').slice(1).join(' ') }} />
                <span className="muted">{c.title || ''}{c.company ? ` · ${c.company}` : ''}</span>
                <span className={`badge engagement-${c.engagement_state}`}>
                  {c.engagement_state === 'suppressed' ? 'do not contact'
                    : `paused${c.paused_until ? ` → ${c.paused_until}` : ''}`}
                </span>
                {c.engagement_note && <span className="muted excl-note">“{c.engagement_note}”</span>}
                {c.engagement_updated_at && (
                  <span className="muted excl-when">
                    since {String(c.engagement_updated_at).slice(0, 10)}
                  </span>
                )}
                <button className="ghost sm" disabled={!!busy}
                  title="Turns outreach back on for this person, everywhere."
                  onClick={() => act(c.contact_id, () => api.updateEngagement({
                    contact_id: c.contact_id, engagement_state: 'active' }))}>
                  {busy === c.contact_id ? <Spinner /> : 'Turn outreach back on'}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
