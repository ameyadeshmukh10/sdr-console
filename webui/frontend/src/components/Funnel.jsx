import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, num, pct } from './ui.jsx'
import { BandMix } from './campaignShared.jsx'
import { useHighlight, rowProps } from './crossHighlight.jsx'
import Addon from './Addon.jsx'
import { Link } from 'react-router-dom'

// The end-to-end funnel.
//
// Analytics used to start at "contacted", because the Bison campaign was the unit of
// analysis. That made every upstream decision invisible — who we chose, why they
// ranked, which channel — so a bad number had no diagnosable cause. This spans the
// whole chain, which means a drop-off can be pinned on the step that caused it.
//
// Two data sources with different freshness, labelled rather than blended: stages up
// to Enrolled are our own tables and exact; Contacted onward come from Bison's last
// stats refresh. A funnel that quietly mixed a live count with a day-old one would
// invent conversion rates.

const SOURCE_LABEL = {
  console: 'from the pipeline — exact',
  bison: 'from the last Bison stats refresh',
}

// Stage id -> the column in the per-campaign table that holds the same number.
// The two are the same measurement at different grain, so picking a stage marks
// the column it decomposes into rather than only lighting up its own card.
const STAGE_COL = {
  qualified: 'qualified', enrolled: 'enrolled', contacted: 'contacted',
  replied: 'replied', interested: 'interested',
}

export default function Funnel({ compact = false, focusCampaign = null, hl = null }) {
  const [data, setData] = useState(null)
  // Shared with CampaignAnalytics when the page passes one in — the same campaign
  // appears in both tables, and picking it in either should mark it in both.
  const own = useHighlight()
  const h = hl || own
  useEffect(() => { api.funnel().then(setData).catch(() => setData({ available: false })) }, [])

  if (!data) return <Spinner label="Loading funnel…" />
  if (!data.available || !(data.stages || []).length) return null

  const stages = data.stages
  const top = stages[0]?.n || 1
  const drop = data.biggest_drop
  const selStage = h.sel?.dim === 'stage' ? h.sel.value : null
  const selCampaign = h.sel?.dim === 'campaign' ? h.sel.value : focusCampaign
  // A picked stage tints its column; nothing is dimmed, because the other columns
  // are the context that makes the highlighted one mean something.
  const colCls = (id) => (selStage && STAGE_COL[selStage] === id ? ' col-on' : '')

  return (
    <>
      <div className="card-h" style={{ marginBottom: 10 }}>
        <div>
          <h2 className="section-h" style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 8 }}>End to end <Addon id="advanced-analytics" /></h2>
          <p className="card-note">
            {drop && drop.of_prev != null ? (
              <>The steepest fall is <b>{stages[stages.findIndex((s) => s.id === drop.id) - 1]?.label}
                {' → '}{drop.label}</b>, holding {pct(drop.of_prev)}. That is where to look first.</>
            ) : 'Every stage of the chain, from who qualified to who came back interested.'}
          </p>
        </div>
      </div>

      <div className="funnel">
        {stages.map((s) => (
          <div key={s.id}
            className={('fstage xh-pick' + h.on(selStage === s.id, 'stage')).trim()}
            role="button" tabIndex={0} aria-pressed={selStage === s.id}
            title={`Show ${s.label.toLowerCase()} per campaign`}
            onClick={() => h.pick('stage', s.id, s.label)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); h.pick('stage', s.id, s.label) }
            }}>
            <div className="fstage-h">
              <span className="fstage-l">{s.label}</span>
              {s.of_prev != null ? (
                <span className={'fstage-c' + (drop && s.id === drop.id ? ' worst' : '')}>
                  {pct(s.of_prev)}
                </span>
              ) : s.mixed ? (
                <span className="fstage-c" style={{ color: 'var(--amber)' }}
                  title="Not comparable to the stage before it — see the note.">n/a</span>
              ) : null}
            </div>
            <div className="fstage-v">{num(s.n)}</div>
            <div className="fstage-bar">
              <span style={{ width: `${Math.max(2, (100 * s.n) / top)}%` }} />
            </div>
            <div className="hint" title={SOURCE_LABEL[s.source]}>
              {s.note || SOURCE_LABEL[s.source]}
            </div>
          </div>
        ))}
      </div>

      {data.mixed_population && (
        <p className="hint" style={{ marginTop: 10 }}>
          The Bison campaigns these bind to also hold leads enrolled before campaigns
          existed, so <b>Contacted</b> counts a wider population than <b>Enrolled</b>.
          No conversion rate is shown between them rather than a misleading one — the
          send-side snapshot has no per-lead attribution to net that out.
        </p>
      )}

      {data.unjoined?.length > 0 && (
        <p className="hint" style={{ marginTop: 10 }}>
          {data.unjoined.length} campaign{data.unjoined.length > 1 ? 's have' : ' has'} no Bison
          campaign bound ({data.unjoined.slice(0, 3).join(', ')}
          {data.unjoined.length > 3 ? '…' : ''}), so their sends aren't counted past Enrolled.
        </p>
      )}

      {!compact && (data.campaigns || []).length > 0 && (
        <div className="panel" style={{ padding: 0, overflowX: 'auto', marginTop: 18 }}>
          <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 940 }}>
            <thead><tr>
              <th style={{ width: '22%' }}>Campaign</th>
              <th style={{ width: '8%' }} className="num">Accounts</th>
              <th style={{ width: '9%' }} className={'num' + colCls('qualified')}>Qualified</th>
              <th style={{ width: '9%' }} className={'num' + colCls('enrolled')}>Enrolled</th>
              <th style={{ width: '9%' }} className={'num' + colCls('contacted')}>Contacted</th>
              <th style={{ width: '8%' }} className={'num' + colCls('replied')}>Replied</th>
              <th style={{ width: '9%' }} className={'num' + colCls('interested')}>Interested</th>
              <th style={{ width: '26%' }}>Priority mix</th>
            </tr></thead>
            <tbody>
              {data.campaigns.map((c) => (
                <tr key={c.campaign_id} {...rowProps({
                  on: h.on, pick: h.pick, dim: 'campaign', value: c.campaign_id,
                  label: c.name, isMatch: selCampaign === c.campaign_id,
                })}>
                  <td>
                    {/* Straight back to the campaign that produced the row. A funnel
                        that diagnoses a drop-off and then makes you go find the
                        campaign by hand stops one step short of useful. The link
                        stops the click so navigating away isn't also a selection. */}
                    <Link to={`/campaigns?open=${c.campaign_id}`}
                      onClick={(e) => e.stopPropagation()}
                      style={{ fontWeight: 600 }} title="Open this campaign">
                      {c.name}
                    </Link>
                    {!c.joined && <div className="acct-dom">no send data</div>}
                  </td>
                  <td className="num">{num(c.accounts)}</td>
                  <td className={'num' + colCls('qualified')}>{num(c.qualified)}</td>
                  <td className={'num' + colCls('enrolled')}>{num(c.enrolled)}</td>
                  <td className={'num' + colCls('contacted')}>
                    {c.joined ? num(c.contacted) : <span className="muted">—</span>}</td>
                  <td className={'num' + colCls('replied')}>
                    {c.joined ? num(c.replied) : <span className="muted">—</span>}</td>
                  <td className={'num' + colCls('interested')}>
                    {c.joined ? num(c.interested) : <span className="muted">—</span>}</td>
                  <td><BandMix bands={c.by_band} avg={c.avg_score} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
