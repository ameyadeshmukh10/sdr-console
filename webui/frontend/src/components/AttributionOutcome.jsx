import { Stat, num } from './ui.jsx'
import { useHighlight, SelectionBar, rowProps } from './crossHighlight.jsx'
import { BRAND } from '../theme.js'

// The Outcome tab: what the AI SDR produced, and the evidence under it.
//
// Four widgets, all cuts of ONE set of deals — the headline tiles, the
// originated/influenced split, the stage breakdown and the deal list. Picking a
// value in any of them marks the matching part of the others, because "which stage
// is the influenced pipeline sitting in?" was previously a question you answered by
// holding two tables in your head.
//
// The summary in the selection strip comes from the SERVER aggregate for stage and
// motion, never from summing the visible rows: the deal list is capped at the 25
// largest, so a row sum would quietly under-report the moment a portal has more.

const usd = (v) => (v == null ? '—' : Number(v).toLocaleString('en-US', {
  style: 'currency', currency: 'USD', maximumFractionDigits: 0,
}))

// One date format for the whole tab. toLocaleString() prints seconds, which is
// noise on a nightly job and made two sync stamps on one screen look like they
// disagreed about precision.
const when = (v) => (v ? new Date(v).toLocaleString(undefined, {
  day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
}) : null)

const MOTIONS = [
  ['originated', 'Outbound-originated',
    'Every qualifying contact came in cold — the AI SDR created this pipeline.'],
  ['influenced', 'Outbound-influenced',
    'At least one contact arrived inbound. Real pipeline we touched, not pipeline we created.'],
  ['unclassified', 'Unclassified',
    'Synced before contact provenance was captured — reclassified on the next sweep.'],
]

const motionOf = (d) => d.attribution || 'unclassified'

export default function AttributionOutcome({ aisdr, syncMsg }) {
  const { sel, pick, clear, on, is } = useHighlight()

  const deals = aisdr?.deals || []
  const byStage = aisdr?.by_stage || []
  const byMotion = aisdr?.by_attribution || {}
  const unavailable = aisdr?.configured === false || aisdr?.error

  // One deal carries both a stage and a motion, so it is the join between the two
  // aggregates: selecting either one resolves through the deal list.
  const selDeal = sel?.dim === 'deal' ? deals.find((d) => d.id === sel.value) : null

  const dealMatch = (d) => {
    if (!sel) return false
    if (sel.dim === 'stage') return d.stage === sel.value
    if (sel.dim === 'motion') return motionOf(d) === sel.value
    if (sel.dim === 'deal') return d.id === sel.value
    return false
  }
  const stageMatch = (stage) => {
    if (!sel) return false
    if (sel.dim === 'stage') return stage === sel.value
    if (sel.dim === 'deal') return selDeal?.stage === stage
    // Which stages does the selected motion actually reach? Answerable only from
    // the listed deals, so it is a "contains one of the largest" claim — accurate
    // for what it marks, and never used for a number.
    if (sel.dim === 'motion') return deals.some((d) => d.stage === stage && motionOf(d) === sel.value)
    return false
  }
  const motionMatch = (key) => {
    if (!sel) return false
    if (sel.dim === 'motion') return key === sel.value
    if (sel.dim === 'deal') return selDeal ? motionOf(selDeal) === key : false
    if (sel.dim === 'stage') return deals.some((d) => motionOf(d) === key && d.stage === sel.value)
    return false
  }

  // Exact where an aggregate exists; otherwise the one deal that was clicked.
  function summary() {
    if (!sel) return null
    if (sel.dim === 'stage') {
      const r = byStage.find((s) => s.stage === sel.value)
      return r ? `${num(r.deals)} ${r.deals === 1 ? 'deal' : 'deals'} · ${usd(r.amount)}` : null
    }
    if (sel.dim === 'motion') {
      const r = byMotion[sel.value]
      return r ? `${num(r.deals)} ${r.deals === 1 ? 'deal' : 'deals'} · ${usd(r.amount)}` : null
    }
    if (selDeal) return `${selDeal.stage} · ${motionOf(selDeal)} · ${usd(selDeal.amount)}`
    return null
  }

  const listedTotal = deals.length
  const allDeals = aisdr?.deals_created ?? listedTotal
  const capped = allDeals > listedTotal

  return (
    <>
      <div className="section-h" style={{ marginBottom: 8 }}>AI SDR pipeline</div>
      {syncMsg && <div className="banner info">{syncMsg}</div>}

      <div className="grid stat-grid" style={{ marginBottom: 24 }}>
        <Stat
          accent
          label="Deals created by AI SDR"
          value={unavailable ? '—' : num(aisdr?.deals_created)}
          sub={aisdr?.configured === false
            ? 'Set MONGO_URL to enable deal attribution'
            : aisdr?.error
              ? `Attribution store unreachable: ${aisdr.error}`
              : 'Associated with a contact we emailed, created after that email'}
        />
        <Stat
          accent
          label="Total pipeline"
          value={unavailable ? '—' : usd(aisdr?.total_pipeline)}
          sub={aisdr?.configured === false
            ? 'HubSpot deal attribution not configured'
            : aisdr?.last_error
              ? `Last sync error: ${aisdr.last_error}`
              : aisdr?.last_sync_at
                ? `Attribution synced ${when(aisdr.last_sync_at)}`
                : 'No sync has run yet — click Refresh'}
        />
      </div>

      {(byStage.length > 0 || Object.keys(byMotion).length > 0) && (
        <SelectionBar sel={sel} clear={clear} summary={summary()}
          hint="Click a motion, a stage or a deal to see where it sits in the others." />
      )}

      {/* Motion split — the honesty guard on the headline number. An inbound-sourced
          contact we also emailed produces an INFLUENCED deal, not an originated one;
          conflating them is what gets the whole figure challenged. */}
      {Object.keys(byMotion).length > 0 && (
        <div className="panel" style={{ marginBottom: 24 }}>
          <div className="section-h" style={{ marginTop: 0 }}>Outbound-originated vs influenced</div>
          <div className="grid stat-grid">
            {MOTIONS.map(([key, label, why]) => {
              const r = byMotion[key]
              if (!r) return null
              const match = motionMatch(key)
              return (
                <button key={key} type="button"
                  className={('stat xh-pick' + on(match, ['stage', 'motion', 'deal'])).trim()}
                  data-tone={key === 'originated' ? 'good' : undefined}
                  aria-pressed={is('motion', key)}
                  onClick={() => pick('motion', key, label)}>
                  <div className="label">{label}</div>
                  <div className="value">{usd(r.amount)}</div>
                  <div className="sub">{num(r.deals)} {r.deals === 1 ? 'deal' : 'deals'} · {why}</div>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {byStage.length > 0 && (
        <div className="panel" style={{ marginBottom: 24 }}>
          <div className="section-h" style={{ marginTop: 0 }}>Where that pipeline sits</div>
          <div className="stage-bars">
            {byStage.map((r) => {
              const share = aisdr.total_pipeline
                ? (100 * (r.amount || 0)) / aisdr.total_pipeline : 0
              const match = stageMatch(r.stage)
              return (
                <div key={r.stage}
                  className={('stage-row xh-pick' + on(match, ['stage', 'motion', 'deal'])).trim()}
                  role="button" tabIndex={0} aria-pressed={is('stage', r.stage)}
                  onClick={() => pick('stage', r.stage, r.stage)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault(); pick('stage', r.stage, r.stage)
                    }
                  }}>
                  <span className="stage-name">{r.stage}</span>
                  <span className="stage-bar"><span style={{ width: `${share}%` }} /></span>
                  <span className="stage-deals">{num(r.deals)} {r.deals === 1 ? 'deal' : 'deals'}</span>
                  <span className="stage-amt">{usd(r.amount)}</span>
                </div>
              )
            })}
          </div>

          {deals.length > 0 && (
            <>
              <div className="section-h">Attributed deals</div>
              <div style={{ overflowX: 'auto' }}>
                <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 820 }}>
                  <thead><tr>
                    <th style={{ width: '30%' }}>Deal</th>
                    <th style={{ width: '15%' }}>Stage</th>
                    <th style={{ width: '12%' }}>Motion</th>
                    <th style={{ width: '12%' }}>Created</th>
                    <th style={{ width: '10%' }} className="num">Contacts</th>
                    <th style={{ width: '13%' }}>Owner</th>
                    <th style={{ width: '13%' }} className="num">Amount</th>
                  </tr></thead>
                  <tbody>
                    {deals.map((d) => (
                      <tr key={d.id} {...rowProps({
                        on, pick, dim: 'deal', value: d.id, reflects: ['stage', 'motion', 'deal'],
                        label: d.name || 'unnamed deal', isMatch: dealMatch(d),
                      })}>
                        <td className="trunc-1" title={d.name || ''}>
                          {d.name || <span className="muted">(unnamed)</span>}
                        </td>
                        <td><span className="badge trunc-1" title={d.stage}>{d.stage}</span></td>
                        <td>{d.attribution
                          ? <span className="badge" style={{
                            color: d.attribution === 'originated' ? BRAND.jade : BRAND.amber,
                            borderColor: d.attribution === 'originated' ? BRAND.jade : BRAND.amber,
                          }}>{d.attribution}</span>
                          : <span className="muted">—</span>}</td>
                        <td className="muted">{d.created_at
                          ? new Date(d.created_at).toLocaleDateString(undefined,
                            { day: 'numeric', month: 'short', year: 'numeric' }) : '—'}</td>
                        <td className="muted num">{num(d.contacts)}</td>
                        <td className="muted trunc-1" title={d.owner || ''}>{d.owner || '—'}</td>
                        <td className="num" style={{ fontWeight: 600 }}>{usd(d.amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
                {capped && (
                  <>Showing the <b>{num(listedTotal)}</b> largest of {num(allDeals)} attributed
                    deals; the tiles and bars above cover all of them.{' '}</>
                )}
                A deal is attributed when it is associated with a contact the AI SDR
                emailed and was created after that first email. <b>Originated</b> means
                every qualifying contact came in cold; <b>influenced</b> means at least
                one arrived inbound, per HubSpot&apos;s original-source. Closed-lost is
                included in the total.
              </p>
            </>
          )}
        </div>
      )}
    </>
  )
}
