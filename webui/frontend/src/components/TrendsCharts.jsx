import { useState } from 'react'
import {
  Bar, BarChart, CartesianGrid, Cell, ComposedChart, Line, ReferenceLine,
  ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis,
} from 'recharts'
import { BRAND, SERIES, TOOLTIP_STYLE } from '../theme.js'
import { num } from './ui.jsx'

// Confidence gate. With interested rates well under 1%, a segment can post a
// flattering percentage off a single reply — so every rate here carries a Wilson
// 95% interval and a verdict, and thin cells are drawn greyed rather than ranked
// as winners. Without this the charts would confidently mislead.
const MIN_INTERESTED = 5
const MIN_CONTACTED = 200

export function wilsonCI(k, n, z = 1.96) {
  if (!n || k < 0) return null
  const p = k / n
  const d = 1 + (z * z) / n
  const c = (p + (z * z) / (2 * n)) / d
  const h = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / d
  return { lo: Math.max(0, (c - h) * 100), hi: Math.min(100, (c + h) * 100) }
}

export function confidence(interested, contacted) {
  if (!contacted) return { level: 'none', label: 'no sends' }
  if (interested < 1) return { level: 'none', label: 'no conversions yet' }
  if (interested < MIN_INTERESTED || contacted < MIN_CONTACTED) {
    return { level: 'low', label: `thin data (n=${interested})` }
  }
  return { level: 'ok', label: `n=${interested}` }
}

const fmtRate = (v) => (v == null ? '—' : `${Number(v).toFixed(v < 1 ? 2 : 1)}%`)

// `chart` is the only thing given a fixed height — legends and the "Act on it"
// line must stay in normal flow, or they overflow the panel and collide with the
// next section.
function Panel({ title, sub, action, height = 320, chart, children }) {
  return (
    <div className="panel" style={{ marginBottom: 22 }}>
      <div className="row between" style={{ alignItems: 'flex-start', gap: 12 }}>
        <div>
          <div className="section-h" style={{ marginTop: 0, marginBottom: sub ? 2 : 10 }}>{title}</div>
          {sub && <p className="muted" style={{ fontSize: 12, margin: '0 0 10px', maxWidth: 620 }}>{sub}</p>}
        </div>
        {action}
      </div>
      {chart && <div style={{ height }}>{chart}</div>}
      {children}
    </div>
  )
}

// A single line naming what to change when a chart shows something. Trends that
// don't point at a knob in Setup are just decoration.
export function Action({ children }) {
  return (
    <p style={{
      fontSize: 12.5, margin: '12px 0 0', paddingTop: 10, lineHeight: 1.55,
      borderTop: `1px solid ${BRAND.border}`, color: BRAND.ink,
    }}>
      <b style={{ color: BRAND.jade }}>Act on it → </b>{children}
    </p>
  )
}

function Segmented({ options, value, onChange }) {
  return (
    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
      {options.map((o) => {
        const on = o.value === value
        return (
          <button key={o.value} onClick={() => onChange(o.value)}
            style={{
              padding: '4px 10px', fontSize: 11.5, borderRadius: 6,
              border: `1px solid ${on ? BRAND.jade : BRAND.border}`,
              background: on ? 'rgba(34,130,111,0.10)' : 'transparent',
              color: on ? BRAND.jade : BRAND.muted, fontWeight: on ? 600 : 500,
              filter: 'none', boxShadow: 'none',
            }}>
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 1. Offer opportunity scatter — volume vs rate. Answers "where do I move sends?"
//    in one read: up-and-left is a starved winner, down-and-right is burning list.
// ---------------------------------------------------------------------------
export function OfferScatter({ byOfferType, overall, hl = null }) {
  if (!byOfferType) return null
  // `hl` is the page's offer selection. The scatter, the "By offer type"
  // distribution and the conversion-by-campaign table are three views of the same
  // offers, so a point picked here is marked in the other two.
  const selOffer = hl?.sel?.dim === 'offer' ? hl.sel.value : null
  const baseline = overall?.interested_rate_pct ?? 0
  const points = Object.entries(byOfferType)
    .filter(([, v]) => (v.contacted || 0) > 0)
    .map(([name, v]) => {
      const conf = confidence(v.interested || 0, v.contacted || 0)
      const ci = wilsonCI(v.interested || 0, v.contacted || 0)
      const above = (v.interested_rate_pct || 0) >= baseline
      return {
        name, contacted: v.contacted, interested: v.interested,
        rate: v.interested_rate_pct || 0, ci, conf, above,
        fill: conf.level !== 'ok' ? 'rgba(15,28,24,0.28)' : above ? BRAND.jade : BRAND.red,
      }
    })
  if (!points.length) return null

  // Pin the log axis to decade boundaries rather than leaving it 'auto': volume
  // spans two or three orders of magnitude here, and fixed decades keep the tick
  // labels stable as offers come and go.
  const vols = points.map((p) => p.contacted)
  const xDomain = [
    Math.pow(10, Math.floor(Math.log10(Math.max(1, Math.min(...vols))))),
    Math.pow(10, Math.ceil(Math.log10(Math.max(...vols)))),
  ]
  const maxRate = Math.max(baseline, ...points.map((p) => p.rate))

  const scaleUp = points.filter((p) => p.above && p.conf.level === 'ok')
    .sort((a, b) => b.rate - a.rate)
  const wasted = points.filter((p) => !p.above && p.conf.level === 'ok')
    .sort((a, b) => b.contacted - a.contacted)
  const thin = points.filter((p) => p.conf.level !== 'ok')

  return (
    <Panel
      title="Offer opportunity — volume vs conversion"
      sub={`Bubble size = interested replies. Dashed line = overall rate (${fmtRate(baseline)}).
        Above the line and to the left = converting well on too few sends. Below and to the
        right = consuming the list without converting. Grey = too thin to rank.`}
      height={340}
      chart={
        <ResponsiveContainer width="100%" height="100%">
        <ScatterChart data={points} margin={{ top: 12, right: 24, bottom: 34, left: 4 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={BRAND.grid} />
          <XAxis
            type="number" dataKey="contacted" scale="log" domain={xDomain} allowDataOverflow
            tick={{ fill: BRAND.muted, fontSize: 11 }} tickFormatter={num}
            label={{ value: 'Leads contacted (log scale)', position: 'insideBottom',
              offset: -20, fill: BRAND.muted, fontSize: 11 }}
          />
          <YAxis
            type="number" dataKey="rate" domain={[0, Math.ceil(maxRate * 1.15 * 10) / 10]}
            tick={{ fill: BRAND.muted, fontSize: 11 }}
            tickFormatter={(v) => `${v}%`}
            label={{ value: 'Interested rate', angle: -90, position: 'insideLeft',
              fill: BRAND.muted, fontSize: 11 }}
          />
          <ZAxis type="number" dataKey="interested" range={[80, 900]} />
          <ReferenceLine y={baseline} stroke={BRAND.muted} strokeDasharray="5 4" />
          <Tooltip
            contentStyle={TOOLTIP_STYLE} cursor={{ strokeDasharray: '3 3' }}
            content={({ payload }) => {
              const p = payload?.[0]?.payload
              if (!p) return null
              return (
                <div style={{ ...TOOLTIP_STYLE, padding: '10px 12px', fontSize: 12.5 }}>
                  <b>{p.name}</b>
                  <div style={{ marginTop: 6 }}>
                    {fmtRate(p.rate)} interested · {num(p.interested)} of {num(p.contacted)}
                  </div>
                  {p.ci && (
                    <div className="muted" style={{ fontSize: 11.5, marginTop: 3 }}>
                      95% CI {fmtRate(p.ci.lo)}–{fmtRate(p.ci.hi)}
                    </div>
                  )}
                  <div style={{
                    fontSize: 11.5, marginTop: 5,
                    color: p.conf.level === 'ok' ? BRAND.jade : BRAND.amber,
                  }}>
                    {p.conf.level === 'ok' ? '' : '⚠ '}{p.conf.label}
                  </div>
                </div>
              )
            }}
          />
          <Scatter data={points} shape="circle" isAnimationActive={false}
            cursor={hl ? 'pointer' : undefined}
            onClick={hl ? (d) => hl.pick('offer', d.name, d.name) : undefined}>
            {points.map((p) => {
              const dim = selOffer && selOffer !== p.name
              return (
                <Cell key={p.name} fill={p.fill} fillOpacity={dim ? 0.12 : 0.62}
                  stroke={p.fill} strokeOpacity={dim ? 0.25 : 1}
                  strokeWidth={selOffer === p.name ? 2.5 : 1.5} />
              )
            })}
          </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
      }
    >
      {/* The legend doubles as the picker — the points themselves are small targets
          and a two-letter offer name is easier to aim at than its bubble. */}
      <div className="row" style={{ gap: 18, flexWrap: 'wrap', fontSize: 11.5, marginTop: 8 }}>
        {points.map((p) => (
          <span key={p.name}
            className={hl ? ('xh-pick' + hl.on(selOffer === p.name, 'offer')).trim() : undefined}
            role={hl ? 'button' : undefined} tabIndex={hl ? 0 : undefined}
            onClick={hl ? () => hl.pick('offer', p.name, p.name) : undefined}
            onKeyDown={hl ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); hl.pick('offer', p.name, p.name) }
            } : undefined}
            style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{
              width: 9, height: 9, borderRadius: '50%', background: p.fill, opacity: 0.75,
            }} />
            {p.name} <span className="muted">{fmtRate(p.rate)}</span>
          </span>
        ))}
      </div>
      <Action>
        {scaleUp.length > 0 && (
          <>Shift volume toward <b>{scaleUp[0].name}</b> ({fmtRate(scaleUp[0].rate)} on just{' '}
            {num(scaleUp[0].contacted)} contacted). </>
        )}
        {wasted.length > 0 && (
          <>Rewrite or retire <b>{wasted[0].name}</b> — {num(wasted[0].contacted)} contacted at{' '}
            {fmtRate(wasted[0].rate)}. </>
        )}
        {thin.length > 0 && (
          <>Hold judgement on {thin.map((p) => p.name).join(', ')} until{' '}
            {MIN_INTERESTED}+ conversions land. </>
        )}
        Offer mix is set by which campaigns you enroll into on the Use view; the pitch behind
        each offer lives in <code>offer.md</code> and <code>cta-offers.md</code>.
      </Action>
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// 2. Sequence funnel — sends decay across steps while the rate line moves
//    independently. Bars alone hide that the cheapest step converts best.
// ---------------------------------------------------------------------------
export function SequenceFunnel({ byStep, overall }) {
  if (!byStep || !Object.keys(byStep).length) return null
  const baseline = overall?.interested_rate_pct ?? 0
  const rows = Object.entries(byStep)
    .sort((a, b) => Number(a[0]) - Number(b[0]))
    .map(([step, v]) => ({
      step: `Step ${step}`, stepNum: Number(step),
      contacted: v.contacted || 0, interested: v.interested || 0,
      rate: v.interested_rate_pct ?? 0,
      conf: confidence(v.interested || 0, v.contacted || 0),
    }))
  const scored = rows.filter((r) => r.conf.level === 'ok')
  const best = scored.length ? scored.reduce((a, b) => (b.rate > a.rate ? b : a)) : null
  const worst = scored.length ? scored.reduce((a, b) => (b.rate < a.rate ? b : a)) : null

  return (
    <Panel
      title="Sequence shape — reach vs conversion by step"
      sub="Bars are how many leads each step reached; the line is the interested rate for that
        step. A step whose bar is tall but whose line is on the floor is spending sends and
        goodwill without converting."
      height={330}
      chart={
        <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={rows} margin={{ top: 12, right: 8, bottom: 8, left: -6 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={BRAND.grid} />
          <XAxis dataKey="step" tick={{ fill: BRAND.muted, fontSize: 11.5 }} />
          <YAxis yAxisId="l" tick={{ fill: BRAND.muted, fontSize: 11 }} tickFormatter={num} />
          <YAxis yAxisId="r" orientation="right" tick={{ fill: BRAND.jade, fontSize: 11 }}
            tickFormatter={(v) => `${v}%`} />
          <ReferenceLine yAxisId="r" y={baseline} stroke={BRAND.muted} strokeDasharray="5 4" />
          <Tooltip
            contentStyle={TOOLTIP_STYLE} cursor={{ fill: 'rgba(15,28,24,0.04)' }}
            formatter={(v, k) => (k === 'Interested rate' ? fmtRate(v) : num(v))}
          />
          <Bar yAxisId="l" dataKey="contacted" name="Leads reached"
            fill={BRAND.jade} fillOpacity={0.2} radius={[3, 3, 0, 0]} />
          <Line yAxisId="r" type="monotone" dataKey="rate" name="Interested rate"
            stroke={BRAND.jade} strokeWidth={2.5}
            dot={{ r: 4, fill: BRAND.jade }} activeDot={{ r: 6 }} />
          </ComposedChart>
        </ResponsiveContainer>
      }
    >
      {best && worst && best.stepNum !== worst.stepNum && (
        <Action>
          <b>{worst.step}</b> is the trough at {fmtRate(worst.rate)} while <b>{best.step}</b> converts
          at {fmtRate(best.rate)} — {(best.rate / (worst.rate || 1)).toFixed(1)}× better on{' '}
          {num(best.contacted)} sends. Rewrite the {worst.step.toLowerCase()} angle and CTA in the
          4-touch table in <code>icp-email.md</code> and the cadence list in{' '}
          <code>cta-offers.md</code>, and carry the change into the persona agents.
        </Action>
      )}
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// 3a. Replies over time + mix. Counts only — this dataset has no per-period
//     denominator, so it must not be presented as a rate (see RateOverTime).
// ---------------------------------------------------------------------------
const DIM_LABELS = {
  offer_type: 'Offer', function: 'Function', seniority: 'Seniority',
  winning_cta: 'Winning CTA', reply_intent: 'Reply intent', winning_step: 'Winning step',
}

export function RepliesOverTime({ timeseries }) {
  const [grain, setGrain] = useState('months')
  const [dim, setDim] = useState('offer_type')
  const [mode, setMode] = useState('count')
  if (!timeseries?.available) {
    return (
      <Panel title="Interested replies over time" height="auto">
        <div className="empty">{timeseries?.note || 'No dated replies available.'}</div>
      </Panel>
    )
  }
  const rows = timeseries[grain] || []
  const key = `by_${dim}`
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r[key] || {})))]
  const data = rows.map((r) => {
    const buckets = r[key] || {}
    const total = Object.values(buckets).reduce((a, b) => a + b, 0)
    const out = { period: grain === 'weeks' ? r.period.slice(5) : r.period, _total: r.replies }
    keys.forEach((k) => {
      const v = buckets[k] || 0
      out[k] = mode === 'share' ? (total ? Math.round((1000 * v) / total) / 10 : 0) : v
    })
    return out
  })

  // Mix shift: aggregate the first half of periods against the second half rather
  // than comparing single endpoint periods. An endpoint with 2 replies can show a
  // "-97 pt swing" that is pure noise; halves keep the claim proportionate.
  const withData = rows.filter((r) => r.replies > 0)
  const half = Math.floor(withData.length / 2)
  const shareOfPeriods = (periods) => {
    const agg = {}
    periods.forEach((r) => Object.entries(r[key] || {}).forEach(([k, v]) => {
      agg[k] = (agg[k] || 0) + v
    }))
    const t = Object.values(agg).reduce((a, c) => a + c, 0)
    return {
      shares: t ? Object.fromEntries(Object.entries(agg).map(([k, v]) => [k, (100 * v) / t])) : {},
      n: t,
    }
  }
  const early = shareOfPeriods(withData.slice(0, half))
  const late = shareOfPeriods(withData.slice(half))
  const shifts = keys.map((k) => ({ k, delta: (late.shares[k] || 0) - (early.shares[k] || 0) }))
    .sort((a, b) => b.delta - a.delta)
  const riser = shifts[0]; const faller = shifts[shifts.length - 1]
  // Both halves need enough replies for a share comparison to mean anything.
  const shiftReadable = half > 0 && early.n >= 10 && late.n >= 10

  return (
    <Panel
      title="Interested replies over time"
      sub={timeseries.note}
      height={300}
      action={
        <div style={{ display: 'grid', gap: 6, justifyItems: 'end' }}>
          <Segmented value={grain} onChange={setGrain}
            options={[{ value: 'months', label: 'Monthly' }, { value: 'weeks', label: 'Weekly' }]} />
          <Segmented value={mode} onChange={setMode}
            options={[{ value: 'count', label: 'Counts' }, { value: 'share', label: '% mix' }]} />
          <Segmented value={dim} onChange={setDim}
            options={(timeseries.mix_dims || Object.keys(DIM_LABELS))
              .map((d) => ({ value: d, label: DIM_LABELS[d] || d }))} />
        </div>
      }
      chart={
        <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 4, left: -8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={BRAND.grid} />
          <XAxis dataKey="period" tick={{ fill: BRAND.muted, fontSize: 11 }} interval={0}
            angle={grain === 'weeks' ? -35 : 0} textAnchor={grain === 'weeks' ? 'end' : 'middle'}
            height={grain === 'weeks' ? 46 : 24} />
          <YAxis tick={{ fill: BRAND.muted, fontSize: 11 }}
            tickFormatter={mode === 'share' ? (v) => `${v}%` : num} />
          <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: 'rgba(15,28,24,0.04)' }}
            formatter={(v) => (mode === 'share' ? `${v}%` : num(v))} />
          {keys.map((k, i) => (
            <Bar key={k} dataKey={k} stackId="a" name={k}
              fill={SERIES[i % SERIES.length]} fillOpacity={0.85} />
          ))}
          </BarChart>
        </ResponsiveContainer>
      }
    >
      <div className="row" style={{ gap: 14, flexWrap: 'wrap', fontSize: 11.5, marginTop: 6 }}>
        {keys.map((k, i) => (
          <span key={k} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 9, height: 9, borderRadius: 2, background: SERIES[i % SERIES.length] }} />
            {k}
          </span>
        ))}
      </div>
      {shiftReadable && riser && faller && riser.k !== faller.k && Math.abs(riser.delta) > 5 && (
        <Action>
          {DIM_LABELS[dim] || dim} mix rotated away from <b>{faller.k}</b>{' '}
          ({faller.delta.toFixed(0)} pts) toward <b>{riser.k}</b> (+{riser.delta.toFixed(0)} pts)
          between the first half of the window ({withData[0].period}–{withData[half - 1].period},{' '}
          {early.n} replies) and the second ({withData[half].period}–
          {withData[withData.length - 1].period}, {late.n} replies). Check that against the rate
          chart above — a mix moving toward a worse-converting{' '}
          {(DIM_LABELS[dim] || dim).toLowerCase()} is drift, not progress.
        </Action>
      )}
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// 3b. True rate over time, differenced from snapshots. Kept separate from the
//     count chart above because it is the only honest "are we improving?" view.
// ---------------------------------------------------------------------------
export function RateOverTime({ rateSeries }) {
  if (!rateSeries?.available) {
    return (
      <Panel title="Conversion rate over time" height="auto">
        <div className="empty" style={{ textAlign: 'left' }}>
          <b>Not enough history yet.</b>
          <div className="muted" style={{ fontSize: 12.5, marginTop: 6, lineHeight: 1.5 }}>
            {rateSeries?.note || 'No snapshot history found.'} Bison only reports
            lifetime-to-date counts, so the console has to snapshot them and difference
            consecutive runs. {rateSeries?.snapshots ? `${rateSeries.snapshots} snapshot so far.` : ''}
          </div>
        </div>
      </Panel>
    )
  }
  const data = (rateSeries.points || []).map((p) => ({
    at: (p.fetched_at || '').slice(0, 10),
    window: p.window_interested_rate_pct ?? null,
    cum: p.cum_interested_rate_pct ?? null,
    newContacted: p.new_contacted ?? 0,
    newInterested: p.new_interested ?? 0,
  }))
  const windows = data.filter((d) => d.window != null)
  const latest = windows[windows.length - 1]
  const prev = windows[windows.length - 2]
  const dir = latest && prev ? latest.window - prev.window : null

  return (
    <Panel
      title="Conversion rate over time (differenced snapshots)"
      sub={rateSeries.note}
      height={320}
      chart={
        <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 12, right: 8, bottom: 8, left: -6 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={BRAND.grid} />
          <XAxis dataKey="at" tick={{ fill: BRAND.muted, fontSize: 11 }} />
          <YAxis yAxisId="l" tick={{ fill: BRAND.muted, fontSize: 11 }} tickFormatter={num} />
          <YAxis yAxisId="r" orientation="right" tick={{ fill: BRAND.jade, fontSize: 11 }}
            tickFormatter={(v) => `${v}%`} />
          <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: 'rgba(15,28,24,0.04)' }}
            formatter={(v, k) => (k === 'Newly contacted' ? num(v) : fmtRate(v))} />
          <Bar yAxisId="l" dataKey="newContacted" name="Newly contacted"
            fill={BRAND.jade} fillOpacity={0.14} radius={[3, 3, 0, 0]} />
          <Line yAxisId="r" type="monotone" dataKey="window" name="Rate this window"
            stroke={BRAND.jade} strokeWidth={2.5} dot={{ r: 4, fill: BRAND.jade }} connectNulls />
          <Line yAxisId="r" type="monotone" dataKey="cum" name="Lifetime-to-date rate"
            stroke={BRAND.muted} strokeWidth={1.6} strokeDasharray="5 4" dot={false} />
          </ComposedChart>
        </ResponsiveContainer>
      }
    >
      {dir != null && (
        <Action>
          Latest window converted at <b>{fmtRate(latest.window)}</b>{' '}
          ({dir >= 0 ? '+' : ''}{dir.toFixed(2)} pts vs the previous window) on{' '}
          {num(latest.newContacted)} newly contacted leads. The dashed lifetime line barely
          moves — judge changes to the persona agents, offer mix and sequence on the solid
          line, and annotate what you changed between runs.
        </Action>
      )}
    </Panel>
  )
}
