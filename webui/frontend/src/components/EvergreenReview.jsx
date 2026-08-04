import { useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'

// The end of an evergreen cycle: what it did, and what to change before the next.
//
// The interval on an evergreen campaign is not "how often it relaunches" — it is
// how often somebody is ASKED. That distinction is the whole feature. Targeting
// takes care of itself: a rolling window keeps finding fresh accounts. What decays
// is the message, and a campaign that quietly re-ran the same four emails at the
// next cohort every quarter is the failure this exists to prevent.
//
// So the cycle's numbers sit directly above the field you'd change because of them.
// Confirming without edits is one click — the point is that it was a decision, not
// that it was laborious.

export default function EvergreenReview({ campaign, counts, onRelaunched }) {
  const [brief, setBrief] = useState(campaign.brief || '')
  const [days, setDays] = useState(String(campaign.evergreen_interval_days || 30))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const by = counts?.by_state || {}
  const edited = (brief || '') !== (campaign.brief || '')

  async function relaunch() {
    setBusy(true); setError(null)
    try {
      await api.relaunchCampaign(campaign.campaign_id, {
        brief, window_days: Number(days),
      })
      onRelaunched()
    } catch (e) { setError(e.message); setBusy(false) }
  }

  return (
    <div className="panel review-panel">
      <div className="card-h">
        <div>
          <h3>Cycle {campaign.cycle || 1} finished — ready for the next?</h3>
          <div className="card-note">
            This campaign is evergreen, so it paused instead of ending. Nothing is
            sending until you relaunch it.
          </div>
        </div>
        <div className="card-meta">
          Ran {(campaign.window_start || '').slice(0, 10)} → {(campaign.window_end || '').slice(0, 10)}
        </div>
      </div>

      <ErrorBanner error={error} />

      {/* What the cycle actually did, right above the thing you'd change because
          of it. Judging the message without the result is guessing. */}
      <div className="import-counts" style={{ marginTop: 12 }}>
        <Cell n={counts?.accounts} label="accounts reached" />
        <Cell n={counts?.members} label="contacts" />
        <Cell n={by.enrolled} label="enrolled" />
        <Cell n={by.replied} label="replied" tone={by.replied ? 'good' : undefined} />
      </div>

      <div style={{ marginTop: 14 }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
          The angle for the next cycle
        </div>
        <textarea rows={5} value={brief} onChange={(e) => setBrief(e.target.value)}
          placeholder="What should the next cycle argue? Leave as-is to run the same angle again."
          style={{ width: '100%', resize: 'vertical' }} />
        <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
          Given to the copy generator for every touch. To change the SEQUENCE itself —
          which offer each step carries — use <b>Sequence &amp; offers</b> before
          relaunching.
        </div>
      </div>

      <div className="card-actions">
        <label className="f">
          Next cycle runs{' '}
          <select value={days} onChange={(e) => setDays(e.target.value)} disabled={busy}>
            {[14, 30, 60, 90].map((d) => <option key={d} value={d}>{d} days</option>)}
          </select>
        </label>
        <button className="primary sm" disabled={busy} onClick={relaunch}>
          {busy ? <Spinner /> : edited ? 'Relaunch with these changes' : 'Relaunch unchanged'}
        </button>
        <span className="hint">
          Opens a fresh window from today and re-qualifies. Contacts already worked in
          earlier cycles aren't re-enrolled.
        </span>
      </div>
    </div>
  )
}

function Cell({ n, label, tone }) {
  return (
    <div className="import-count">
      <div className="v" data-tone={tone}>{num(n || 0)}</div>
      <div className="l">{label}</div>
    </div>
  )
}
