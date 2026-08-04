import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, num, pct } from './ui.jsx'
import { StatusBadge, BandMix } from './campaignShared.jsx'
import { useHighlight, rowProps } from './crossHighlight.jsx'

// Console-campaign performance.
//
// The rest of Analytics reports on BISON campaigns — the sending containers. This
// answers a different question: did the way we DEFINED and PRIORITISED an audience
// actually work? The join is 1:1 via bison_campaign_id, so both live on one page
// without double-counting.
//
// The band table is the important one. A priority score that nobody checks is just
// a number: the only honest test is whether hot contacts reply more than cool ones,
// so that comparison is shown rather than asserted.

export default function CampaignAnalytics({ hl = null }) {
  const [data, setData] = useState(null)
  // Shares the funnel's selection when the page passes one down: the same campaign
  // is a row in both tables, and picking it in one should mark it in the other.
  const own = useHighlight()
  const h = hl || own
  useEffect(() => { api.campaignAnalytics().then(setData).catch(() => setData({ available: false })) }, [])

  if (!data) return <Spinner label="Loading campaigns…" />
  if (!data.available || !(data.campaigns || []).length) return null

  const rows = data.campaigns
  const bands = data.by_band || {}
  const chan = data.by_channel || {}
  const mom = data.momentum || {}
  const order = ['hot', 'warm', 'cool']
  const totalMembers = rows.reduce((a, c) => a + c.members, 0)
  const totalCredits = rows.reduce((a, c) => a + (c.credits || 0), 0)

  // Does priority predict replies? Only claim it when hot actually beats cool.
  // cool = 0% is the strongest separation, not a missing comparison.
  const hotRate = bands.hot?.reply_rate_pct
  const coolRate = bands.cool?.reply_rate_pct
  const predictive = hotRate != null && coolRate != null && hotRate > coolRate

  return (
    <>
      <h2 className="section-h">Campaigns</h2>
      <div className="grid stat-grid" style={{ marginBottom: 20 }}>
        <Stat label="Active campaigns" value={num(rows.filter((c) => c.status === 'active').length)}
          sub={`${num(rows.length)} total`} />
        <Stat label="Contacts targeted" value={num(totalMembers)} />
        <Stat label="Signal warming" value={num(mom.warming || 0)} accent
          sub={`${num(mom.cooling || 0)} cooling · ${num(mom.flat || 0)} flat`} />
        <Stat label="Enrichment credits" value={num(totalCredits)} sub="attributed to campaigns" />
      </div>

      <div className="panel" style={{ padding: 0, overflowX: 'auto', marginBottom: 24 }}>
        <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 940 }}>
          <thead><tr>
            <th style={{ width: '22%' }}>Campaign</th>
            <th style={{ width: '9%' }}>Status</th>
            <th style={{ width: '13%' }}>Audience</th>
            <th style={{ width: '9%' }} className="num">Accounts</th>
            <th style={{ width: '9%' }} className="num">Enrolled</th>
            <th style={{ width: '9%' }} className="num">Replied</th>
            <th style={{ width: '9%' }} className="num">Reply %</th>
            <th style={{ width: '20%' }}>Priority mix</th>
          </tr></thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.campaign_id} {...rowProps({
                on: h.on, pick: h.pick, dim: 'campaign', value: c.campaign_id,
                label: c.name, isMatch: h.is('campaign', c.campaign_id),
              })}>
                <td>
                  <div style={{ fontWeight: 600 }} className="trunc-1" title={c.name}>{c.name}</div>
                  <div className="acct-dom">{num(c.members)} contacts</div>
                </td>
                <td><StatusBadge status={c.status} /></td>
                <td className="muted trunc-1" style={{ fontSize: 12 }}
                  title={c.audience.replace(/_/g, ' ')}>{c.audience.replace(/_/g, ' ')}</td>
                <td className="num">{num(c.accounts)}</td>
                <td className="num">{num(c.enrolled)}</td>
                <td className="num">{num(c.replied)}</td>
                <td className="num">{pct(c.reply_rate_pct)}</td>
                <td><BandMix bands={c.by_band} avg={c.avg_score} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid" style={{ gap: 18, gridTemplateColumns: 'repeat(auto-fit,minmax(320px,1fr))',
        marginBottom: 24 }}>
        <div className="panel">
          <div className="card-h"><h3>Does priority predict replies?</h3></div>
          <p className="card-note">
            {predictive
              ? `Hot contacts reply at ${pct(hotRate)} against ${pct(coolRate)} for cool — the score is sorting the list in the right direction.`
              : 'Not enough replies yet to tell whether the score is sorting in the right direction.'}
          </p>
          <div className="panel-scroll"><table className="dense tight" style={{ width: '100%', marginTop: 12 }}>
            <thead><tr><th>Band</th><th className="num">Contacts</th><th className="num">Enrolled</th>
              <th className="num">Replied</th><th className="num">Reply %</th></tr></thead>
            <tbody>
              {order.filter((b) => bands[b]).map((b) => (
                <tr key={b}>
                  <td><span className={`score ${b}`}>{b}</span></td>
                  <td className="num">{num(bands[b].members)}</td>
                  <td className="num">{num(bands[b].enrolled)}</td>
                  <td className="num">{num(bands[b].replied)}</td>
                  <td className="num">{pct(bands[b].reply_rate_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </div>

        <div className="panel">
          <div className="card-h"><h3>Recommended channel mix</h3></div>
          <p className="card-note">
            How many contacts each channel is recommended for. Sending capacity is finite, so
            this is where it is being pointed.
          </p>
          <div className="panel-scroll"><table className="dense tight" style={{ width: '100%', marginTop: 12 }}>
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
          {data.overlap?.contacts > 0 && (
            <p className="hint" style={{ marginTop: 12 }}>
              {num(data.overlap.contacts)} contacts sit in more than one campaign. Their touches
              are merged into a single spaced cadence, so the channel counts above are people,
              not sends.
            </p>
          )}
        </div>
      </div>
    </>
  )
}
