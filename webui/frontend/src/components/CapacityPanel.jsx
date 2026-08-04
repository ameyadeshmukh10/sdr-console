import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from './ui.jsx'
import Addon, { useAddons } from './Addon.jsx'

// Two scarce things, one ledger.
//
//   CREDITS  Clay and Prospeo bill per call. Real money, spent by background jobs.
//   SENDS    LinkedIn allows ~20 actions per connected account per DAY before the
//            account is at risk; email is capped by the sending plan per MONTH.
//
// Report-only: nothing here blocks an action. The point is that the system can
// spend money and burn a finite send allowance without anyone watching, so the
// number has to be somewhere a human sees it. Different clocks on purpose —
// showing the LinkedIn daily allowance as a monthly figure would hide the
// constraint that actually bites.

function Meter({ label, used, cap, pct, sub, resets, addon }) {
  const p = Math.min(100, pct ?? 0)
  const tone = p >= 90 ? 'meter-bad' : p >= 70 ? 'meter-warn' : 'meter-ok'
  return (
    <div className="panel" style={{ padding: '16px 18px' }}>
      <div className="card-h">
        <h3 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {label}{addon && <Addon id={addon} />}
        </h3>
        <span className="card-meta">resets {resets}</span>
      </div>
      <div className="meter-v">{num(Math.round(used))} <small>/ {num(cap)}</small></div>
      <div className={`meter-bar ${tone}`}><span style={{ width: `${p}%` }} /></div>
      <div className="hint" style={{ marginTop: 8 }}>{sub}</div>
    </div>
  )
}

export default function CapacityPanel() {
  const { credits: creditBudget } = useAddons()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.capacity(30).then(setData).catch((e) => setError(e.message))
  }, [])

  if (error) return <ErrorBanner error={error} />
  if (!data) return <Spinner label="Loading…" />
  if (data.available === false) {
    return <div className="empty">Capacity tracking is not available for this dataset.</div>
  }

  const c = data.capacity || {}
  const s = data.spend || {}
  const li = c.linkedin || {}
  const em = c.email || {}
  const credits = (s.by_provider || []).filter((r) => r.unit_kind === 'credits')
  const cr = c.credits || {}

  return (
    <div>
      <p className="page-sub" style={{ marginTop: 0 }}>
        What the worker is consuming: finite sending allowance, and enrichment credits that
        cost real money. Measured, not enforced — nothing here blocks a run.
      </p>

      <div className="meters" style={{ marginBottom: 24 }}>
        {/* Enrichment credits are the Advanced tier's metered allowance, so the tier
            is named on the meter rather than left implicit. */}
        <Meter label="Enrichment credits" used={cr.used} cap={cr.budget || creditBudget}
          pct={cr.pct} resets="monthly" addon="advanced-analytics"
          sub={`Advanced tier — ${num(cr.budget || creditBudget)} credits per month, across Clay and Prospeo.`} />
        <Meter label="LinkedIn actions" used={li.used} cap={li.per_day} pct={li.pct}
          resets="daily"
          sub={`${num(li.accounts)} connected account${li.accounts === 1 ? '' : 's'} × ${num(li.per_account_day)}/day. Going past this risks the accounts, not just deliverability.`} />
        <Meter label="Email sends" used={em.used} cap={em.per_month} pct={em.pct}
          resets="monthly" sub="Sending-plan ceiling for the calendar month." />
      </div>

      <h2 className="section-h">Enrichment spend — last {s.days || 30} days</h2>
      <p className="card-note" style={{ marginTop: -6, marginBottom: 12 }}>
        Counted against the Advanced tier's {num(cr.budget || creditBudget)}/month allowance.
        Report-only — nothing here blocks a run.
      </p>
      <div className="grid stat-grid" style={{ marginBottom: 18 }}>
        <Stat label="Credits spent" value={num(s.credits)} accent
          sub="Clay + Prospeo combined" />
        {credits.map((r) => (
          <Stat key={r.provider} label={r.provider} value={num(r.units)}
            sub={`${num(r.events)} calls`} />
        ))}
      </div>

      {(s.by_operation || []).length > 0 && (
        <div className="panel" style={{ padding: 0, overflowX: 'auto', marginBottom: 18 }}>
          <table className="dense" style={{ width: '100%' }}>
            <thead><tr>
              <th>Provider</th><th>Operation</th><th>Units</th><th>Calls</th>
            </tr></thead>
            <tbody>
              {s.by_operation.map((r) => (
                <tr key={`${r.provider}-${r.operation}-${r.unit_kind}`}>
                  <td>{r.provider}</td>
                  <td className="muted">{r.operation}</td>
                  <td>{num(r.units)} <span className="muted" style={{ fontSize: 11 }}>{r.unit_kind}</span></td>
                  <td className="muted">{num(r.events)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(s.recent || []).length > 0 && (
        <>
          <h2 className="section-h">Recent activity</h2>
          <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
            <table className="dense" style={{ width: '100%' }}>
              <thead><tr><th>When</th><th>Provider</th><th>Operation</th><th>Units</th><th>Ref</th></tr></thead>
              <tbody>
                {s.recent.slice(0, 20).map((r) => (
                  <tr key={r.id}>
                    <td className="muted" style={{ fontSize: 12 }}>{(r.occurred_at || '').replace('T', ' ').slice(0, 16)}</td>
                    <td>{r.provider}</td>
                    <td className="muted">{r.operation}</td>
                    <td>{num(r.units)} <span className="muted" style={{ fontSize: 11 }}>{r.unit_kind}</span></td>
                    <td className="muted mono" style={{ fontSize: 11 }}>{r.ref || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
