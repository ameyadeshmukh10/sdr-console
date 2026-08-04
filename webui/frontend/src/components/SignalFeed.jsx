import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, num } from './ui.jsx'
import Addon from './Addon.jsx'

// What fired, and when.
//
// The table above this one is the per-company CACHE: one mutable row per domain
// holding the latest research/tech/hiring values. It cannot answer "what happened
// this week", because an upsert destroys the previous value and its timestamps
// record when we LOOKED, not what we found.
//
// This is the event log — append-only, every kind, ordered by when the signal
// actually appeared. It is also the thing campaign windows qualify against, so
// what you see here is literally what a campaign would catch.
//
// Kinds come from campaigns.SIGNAL_REGISTRY: adding one there makes it appear here,
// in the filter, and in the weekly chart with no change to this file.

export default function SignalFeed() {
  const [data, setData] = useState(null)
  const [vocab, setVocab] = useState(null)
  const [kind, setKind] = useState('')
  const [days, setDays] = useState(30)

  useEffect(() => { api.audienceVocab().then(setVocab).catch(() => {}) }, [])
  useEffect(() => { setData(null); api.signalEvents(days).then(setData).catch(() => setData({ available: false })) }, [days])

  const meta = Object.fromEntries((vocab?.signal_kinds || []).map((k) => [k.id, k]))
  const events = (data?.events || []).filter((e) => !kind || e.kind === kind)
  const counts = data?.counts || {}
  const kinds = Object.keys(counts).sort((a, b) => counts[b] - counts[a])

  return (
    <div style={{ marginTop: 34 }}>
      <h2 className="section-h" style={{ marginTop: 0, display: 'flex', alignItems: 'center', gap: 8 }}>Signal activity <Addon id="technographic-agent" /> <Addon id="hiring-agent" /></h2>
      <p className="page-sub" style={{ marginBottom: 18 }}>
        Every signal observation, newest first — the log campaign windows qualify against.
        The cache above holds only the latest value per company; this is what actually fired
        and when.
      </p>

      {data && data.available === false ? (
        <div className="empty">No signal history recorded yet.</div>
      ) : (
        <>
          <div className="grid stat-grid" style={{ marginBottom: 18 }}>
            <Stat label={`Observations (${days}d)`} accent
              value={num(Object.values(counts).reduce((a, b) => a + b, 0))} />
            {kinds.slice(0, 4).map((k) => (
              <Stat key={k} label={meta[k]?.label || k} value={num(counts[k])}
                sub={meta[k]?.detector ? `via ${meta[k].detector}` : null} />
            ))}
          </div>

          <div className="toolbar">
            <label className="field">Signal type
              <select value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="">all kinds</option>
                {(vocab?.signal_kinds || []).map((k) => (
                  <option key={k.id} value={k.id}>{k.label}</option>
                ))}
              </select>
            </label>
            <label className="field">Window
              <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
                {[7, 30, 90, 365].map((d) => <option key={d} value={d}>last {d} days</option>)}
              </select>
            </label>
            <div className="grow" />
            <span className="hint" style={{ alignSelf: 'center' }}>{num(events.length)} shown</span>
          </div>

          {!data ? <Spinner label="Loading…" /> : events.length === 0 ? (
            <div className="empty">Nothing fired in this window.</div>
          ) : (
            <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
              <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 820 }}>
                <thead><tr>
                  <th style={{ width: '13%' }}>When</th>
                  <th style={{ width: '14%' }}>Type</th>
                  <th style={{ width: '20%' }}>Account</th>
                  <th style={{ width: '53%' }}>What fired</th>
                </tr></thead>
                <tbody>
                  {events.map((e) => (
                    <tr key={e.id}>
                      <td className="muted" style={{ fontSize: 12 }}>
                        {(e.observed_at || '').slice(0, 10)}
                      </td>
                      <td>
                        <span className="badge" title={meta[e.kind]?.description}>
                          {meta[e.kind]?.label || e.kind.replace(/_/g, ' ')}
                        </span>
                      </td>
                      <td className="mono" style={{ fontSize: 12 }}>
                        <span className="clamp2">{e.domain}</span>
                      </td>
                      <td className="muted">
                        <span className="clamp2" title={e.summary}>{e.summary}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  )
}
