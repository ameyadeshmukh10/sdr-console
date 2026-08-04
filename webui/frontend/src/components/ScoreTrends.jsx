import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, num, pct } from './ui.jsx'
import Addon from './Addon.jsx'

// Is the targeting model working?
//
// Trends already answers "which copy and which offer earn replies". This answers
// the question one layer up: does the way we PICK and RANK people predict anything?
// Three tests, each shown rather than asserted:
//
//   band       do hot contacts reply more than cool ones
//   channel    which recommended channel actually converts
//   momentum   how much of the book is warming vs cooling
//
// A scoring model nobody checks is just a number on a row, so the honest version of
// this panel is one that can say "not yet" — which it does when the replies are too
// thin to rank.

const MIN_REPLIES = 5   // same confidence bar TrendsCharts uses

export default function ScoreTrends() {
  const [data, setData] = useState(null)
  useEffect(() => {
    api.campaignAnalytics().then(setData).catch(() => setData({ available: false }))
  }, [])

  if (!data) return <Spinner label="Loading…" />
  if (!data.available || !(data.campaigns || []).length) return null

  const bands = data.by_band || {}
  const chan = data.by_channel || {}
  const mom = data.momentum || {}
  const order = ['hot', 'warm', 'cool'].filter((b) => bands[b])
  const totalReplies = order.reduce((a, b) => a + (bands[b].replied || 0), 0)
  const thin = totalReplies < MIN_REPLIES

  // Compare the top band against the bottom. cool = 0% is the STRONGEST possible
  // result, not a missing one, so it must not fall through to "no lift" via a
  // divide-by-zero — that reported a perfectly separating model as a broken one.
  const hot = bands.hot?.reply_rate_pct
  const cool = bands.cool?.reply_rate_pct
  const separates = hot != null && cool != null && hot > cool
  const lift = separates && cool > 0 ? hot / cool : null

  const momTotal = (mom.warming || 0) + (mom.cooling || 0) + (mom.flat || 0)

  return (
    <>
      <h2 className="section-h" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>Is the targeting model working? <Addon id="advanced-analytics" /></h2>
      <p className="muted" style={{ fontSize: 12.5, marginTop: -4, maxWidth: 700 }}>
        {thin ? (
          <>Not enough replies from scored contacts yet to rank the bands against each other —
            {' '}{num(totalReplies)} so far, {MIN_REPLIES} is the floor. The counts below are
            real, the rates are not yet meaningful.</>
        ) : separates ? (
          <>Hot contacts reply at <b>{pct(hot)}</b> against <b>{pct(cool)}</b> for cool
            {lift ? <> — <b>{lift.toFixed(1)}x</b> the rate</> : null}, so the priority score is
            sorting the call list in the right direction.</>
        ) : (
          <>Hot contacts are <b>not</b> replying meaningfully more than cool ones. The score is
            ordering the list but not yet predicting outcomes — worth revisiting the weights
            before trusting the ranking.</>
        )}
      </p>

      <div className="grid" style={{ gap: 18, gridTemplateColumns: 'repeat(auto-fit,minmax(330px,1fr))',
        marginBottom: 22 }}>
        <div className="panel">
          <div className="card-h"><h3>By priority band</h3></div>
          <p className="card-note">
            The test the score has to pass: hot should reply more than cool. Counts are
            contacts scored into each band, not sends.
          </p>
          <div className="panel-scroll"><table className="dense tight" style={{ width: '100%', marginTop: 10 }}>
            <thead><tr><th>Band</th><th className="num">Contacts</th><th className="num">Enrolled</th>
              <th className="num">Replied</th><th className="num">Reply %</th></tr></thead>
            <tbody>
              {order.map((b) => (
                <tr key={b} style={thin ? { opacity: 0.6 } : undefined}>
                  <td><span className={`score ${b}`}>{b}</span></td>
                  <td className="num">{num(bands[b].members)}</td>
                  <td className="num">{num(bands[b].enrolled)}</td>
                  <td className="num">{num(bands[b].replied)}</td>
                  <td className="num"><b>{pct(bands[b].reply_rate_pct)}</b></td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </div>

        <div className="panel">
          <div className="card-h"><h3>By recommended channel</h3></div>
          <p className="card-note">
            Contacts each channel is recommended for. One person can appear in several — this
            is where finite capacity is being pointed, not a send count.
          </p>
          <div className="panel-scroll"><table className="dense tight" style={{ width: '100%', marginTop: 10 }}>
            <thead><tr><th>Channel</th><th className="num">Contacts</th>
              <th className="num">Replied</th><th className="num">Reply %</th></tr></thead>
            <tbody>
              {['call', 'linkedin', 'email', 'ads'].filter((k) => chan[k]).map((k) => (
                <tr key={k}>
                  <td style={{ textTransform: 'capitalize' }}>{k}</td>
                  <td className="num">{num(chan[k].members)}</td>
                  <td className="num">{num(chan[k].replied)}</td>
                  <td className="num">{pct(chan[k].members ? (100 * chan[k].replied) / chan[k].members : null)}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </div>

        <div className="panel">
          <div className="card-h"><h3>Score movement</h3></div>
          <p className="card-note">
            Where the book is heading. Momentum re-ranks the call list, so a mostly-cooling book
            means today's top targets are yesterday's leftovers.
          </p>
          {momTotal > 0 && (
            <div className="mix" style={{ height: 8, margin: '14px 0 10px' }}>
              <span className="hot" style={{ width: `${(100 * (mom.warming || 0)) / momTotal}%` }} />
              <span className="cool" style={{ width: `${(100 * (mom.flat || 0)) / momTotal}%` }} />
              <span className="warm" style={{ width: `${(100 * (mom.cooling || 0)) / momTotal}%` }} />
            </div>
          )}
          <div className="panel-scroll"><table className="dense tight" style={{ width: '100%' }}>
            <tbody>
              <tr><td>Warming</td><td className="num" style={{ color: 'var(--green)' }}><b>{num(mom.warming || 0)}</b></td></tr>
              <tr><td>Flat / first scoring</td><td className="num">{num(mom.flat || 0)}</td></tr>
              <tr><td>Cooling</td><td className="num" style={{ color: 'var(--red)' }}><b>{num(mom.cooling || 0)}</b></td></tr>
            </tbody>
          </table></div>
        </div>
      </div>
    </>
  )
}
