import { useEffect, useState } from 'react'
import { api } from '../api.js'
import ScoreTrends from '../components/ScoreTrends.jsx'
import Reports from '../components/Reports.jsx'
import { useDemo } from '../DemoContext.jsx'
import { useHighlight, SelectionBar, rowProps } from '../components/crossHighlight.jsx'
import { Stat, Spinner, ErrorBanner, num, pct } from '../components/ui.jsx'
import {
  OfferScatter, SequenceFunnel, RepliesOverTime, RateOverTime, confidence,
} from '../components/TrendsCharts.jsx'
import { BRAND, SERIES } from '../theme.js'

// Pillar 5 — Trends: what's working across the interested replies. Reads the
// cached interested-trends analysis; refresh re-runs the fetch + analyze chain.
const COLORS = SERIES

// Horizontal distribution from a {key: {count, pct}} map.
//
// `dim` makes the rows selectable. Only passed where the dimension exists in
// another widget on this page — a bar that highlights only itself teaches people
// the interaction does nothing, so the rest stay static.
function Dist({ title, data, color = BRAND.jade, dim = null, hl = null }) {
  if (!data) return null
  const entries = Object.entries(data).sort((a, b) => (b[1].count || 0) - (a[1].count || 0))
  const max = Math.max(1, ...entries.map(([, v]) => v.count || 0))
  const live = dim && hl
  return (
    <div className="panel">
      <div className="section-h" style={{ marginTop: 0 }}>{title}</div>
      {entries.map(([k, v]) => (
        <div key={k} style={{ marginBottom: 9 }}
          className={live ? ('xh-pick' + hl.on(hl.is(dim, k), dim)).trim() : undefined}
          role={live ? 'button' : undefined} tabIndex={live ? 0 : undefined}
          onClick={live ? () => hl.pick(dim, k, k) : undefined}
          onKeyDown={live ? (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); hl.pick(dim, k, k) }
          } : undefined}>
          <div className="row between" style={{ fontSize: 12.5, marginBottom: 3 }}>
            <span className="trunc-1">{k}</span>
            <span className="muted" style={{ whiteSpace: 'nowrap' }}>{v.count} · {v.pct}%</span>
          </div>
          <div style={{ background: 'var(--panel-2)', borderRadius: 5, height: 8, overflow: 'hidden' }}>
            <div style={{ width: `${(100 * (v.count || 0)) / max}%`, height: '100%', background: color }} />
          </div>
        </div>
      ))}
    </div>
  )
}

// Organised by the DECISION you'd make, not by where the data happens to live.
// Targeting = who we pick and how we rank them. Messaging = what we say to them.
// Previously the score/channel/momentum work was appended as a final section, which
// buried the question ("is the targeting model working?") under the answers to a
// different one.
const TABS = [
  { id: 'targeting', label: 'Targeting' },
  { id: 'messaging', label: 'Messaging' },
  { id: 'data', label: 'Raw data' },
]

export default function TrendsPage() {
  const [tab, setTab] = useState('targeting')
  const [data, setData] = useState(null)
  const [variants, setVariants] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  // Data source is global now (sidebar switcher); this page just reacts to it.
  const { profileId: demo } = useDemo()

  function load() {
    setData(null)
    api.trends().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
    api.variants().then(setVariants).catch(() => {})
  }
  useEffect(() => { load() }, [])

  async function refresh() {
    setRefreshing(true); setError(null)
    try {
      const d = await api.refreshTrends()
      setData(d)
      if (!d.ok) setError('Refresh chain returned errors — showing latest available analysis.')
    } catch (e) { setError(e.message) }
    finally { setRefreshing(false) }
  }

  const s = data?.summary
  const conv = data?.conversion
  const cohorts = data?.cohorts
  const when = data?.fetched_at ? new Date(data.fetched_at).toLocaleString(undefined, {
    day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  }) : '—'

  // Messaging tab: OFFER is the dimension three widgets share — the opportunity
  // scatter, the "By offer type" distribution and every row of the conversion table.
  // Picking one marks it in all three, which is the read the page was asking people
  // to do by eye.
  const hl = useHighlight()
  const selOffer = hl.sel?.dim === 'offer' ? hl.sel.value : null
  const offerStats = selOffer ? conv?.by_offer_type?.[selOffer] : null

  return (
    <div>
      <div className="row between">
        <div>
          <h1 className="page-title">Trends — what's working</h1>
          <p className="page-sub">
            {tab === 'targeting'
              ? 'Whether the way we pick and rank accounts predicts anything.'
              : 'Patterns across interested replies: who replies, to which offer, via which CTA.'}
          </p>
        </div>
        <button onClick={refresh} disabled={refreshing || !!demo}
          title={demo ? 'Switch back to live data to refresh the real analysis' : undefined}>
          {refreshing ? <Spinner label="Refreshing…" /> : '↻ Refresh analysis'}
        </button>
      </div>

      <nav className="uth-tabs" style={{ margin: '10px 0 20px' }}>
        {TABS.map((t) => (
          <button key={t.id} type="button"
            className={'uth-tab' + (tab === t.id ? ' active' : '')}
            onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </nav>

      <ErrorBanner error={error} />

      {tab === 'targeting' && <ScoreTrends />}

      {tab === 'data' && <Reports />}

      {tab === 'messaging' && (<>
      <div className="banner info">
        Based on <b>{num(data?.total_interested || 0)}</b> interested replies · analysis fetched <b>{when}</b>
      </div>

      {variants?.variants?.length > 0 && (
        <div className="panel" style={{ marginBottom: 22 }}>
          <div className="section-h" style={{ marginTop: 0 }}>By instruction variant (A/B)</div>
          <table>
            <thead><tr><th>Variant</th><th className="num">Contacts</th><th className="num">Enrolled</th>
              <th className="num">Interested</th><th className="num">Interested rate</th></tr></thead>
            <tbody>
              {variants.variants.map((v) => (
                <tr key={v.variant}>
                  <td><span className="badge cta">{v.variant}</span></td>
                  <td className="num">{num(v.total)}</td>
                  <td className="num">{num(v.enrolled)}</td>
                  <td className="num">{num(v.interested)}</td>
                  <td className="num"><b>{v.interested_rate_pct == null ? '—' : v.interested_rate_pct + '%'}</b></td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
            Interested = an enrolled contact whose email shows up in the interested set. Pre-variant
            contacts count as the <b>value-give</b> baseline. Run <b>earn</b> / <b>show</b> batches from
            the Pipeline tab to populate the other arms.
          </p>
        </div>
      )}

      {!data ? <Spinner label="Loading…" /> : !data.available ? (
        <div className="empty">No analysis found. Click <b>Refresh analysis</b> to pull replies and build it.</div>
      ) : (
        <>
          <div className="grid stat-grid" style={{ marginBottom: 22 }}>
            <Stat label="Interested replies" value={num(s.total_replies)} sub={`${num(s.genuine_count)} genuine`} />
            <Stat label="Overall interested rate" value={pct(conv?.overall?.interested_rate_pct)}
              sub={`${num(conv?.overall?.interested)} of ${num(conv?.overall?.contacted)}`} accent />
            <Stat label="Avg winning email" value={`${s.winning_email_word_count?.avg ?? '—'}w`}
              sub={`${s.winning_email_word_count?.min}–${s.winning_email_word_count?.max} words`} />
            <Stat label="Meeting/demo accepts" value={num(s.by_reply_intent?.['Meeting/demo accept']?.count || 0)}
              sub={`${s.by_reply_intent?.['Meeting/demo accept']?.pct || 0}% of replies`} />
          </div>

          <h2 className="section-h">Where the rate actually is</h2>
          <p className="muted" style={{ fontSize: 12.5, marginTop: -4, maxWidth: 700 }}>
            These three read against a denominator, so they measure performance rather than
            describing it. Each one ends in the change it implies.
          </p>
          <SelectionBar sel={hl.sel} clear={hl.clear}
            summary={offerStats
              ? `${num(offerStats.interested)} interested of ${num(offerStats.contacted)} contacted `
                + `· ${pct(offerStats.interested_rate_pct)}`
              : 'marked in the scatter, the offer mix and the campaign table'}
            hint="Click an offer — in the scatter below, in the offer mix, or on a campaign row — to follow it across all three." />
          <OfferScatter byOfferType={conv?.by_offer_type} overall={conv?.overall} hl={hl} />
          <SequenceFunnel byStep={conv?.by_step} overall={conv?.overall} />
          <RateOverTime rateSeries={conv?.rate_series} />

          <h2 className="section-h">How the win mix is shifting</h2>
          <p className="muted" style={{ fontSize: 12.5, marginTop: -4, maxWidth: 700 }}>
            Counts of interested replies over time. No denominator here — a rising bar can mean
            better copy or simply more sends, so read it alongside the rate chart above.
          </p>
          <RepliesOverTime timeseries={s.timeseries} />

          <h2 className="section-h">Who replies — composition, not lift</h2>
          <p className="muted" style={{ fontSize: 12.5, marginTop: -4, maxWidth: 700 }}>
            Shares of the {num(s.total_replies)} interested replies — the shape of who replies,
            not evidence that one segment converts better.
          </p>
          {/* Only "By offer type" is selectable: offer is the one dimension here that
              also exists in the scatter and the campaign table. Seniority, function
              and intent have no counterpart on this page, so making them clickable
              would promise a link that isn't there. */}
          <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', marginBottom: 22 }}>
            <Dist title="By seniority" data={s.by_seniority} color={SERIES[0]} />
            <Dist title="By function" data={s.by_function} color={SERIES[1]} />
            <Dist title="Winning CTA type" data={s.by_winning_cta} color={SERIES[3]} />
            <Dist title="Reply intent" data={s.by_reply_intent} color={SERIES[4]} />
            <Dist title="By offer type" data={s.by_offer_type} color={SERIES[5]}
              dim="offer" hl={hl} />
            <Dist title="Interested via" data={s.by_interested_via} color={SERIES[2]} />
          </div>

          {conv?.by_campaign && (
            <>
              <h2 className="section-h">Conversion by campaign</h2>
              <p className="muted" style={{ fontSize: 12.5, marginTop: -4, maxWidth: 700 }}>
                Ranked by interested rate. Rows dimmed with <b>thin</b> haven't converted enough
                to rank against the others — a high percentage on a handful of replies is noise,
                not a winner.
              </p>
              <div className="panel" style={{ padding: 0, marginBottom: 22, overflowX: 'auto' }}>
                <table style={{ tableLayout: 'fixed', width: '100%', minWidth: 900 }}>
                  <thead><tr>
                    <th style={{ width: '22%' }}>Campaign</th>
                    <th style={{ width: '13%' }}>Offer</th>
                    <th style={{ width: '8%' }}>Geo</th>
                    <th style={{ width: '11%' }} className="num">Contacted</th>
                    <th style={{ width: '11%' }} className="num">Interested</th>
                    <th style={{ width: '11%' }} className="num">Interested %</th>
                    <th style={{ width: '9%' }} className="num">Reply %</th>
                    <th style={{ width: '15%' }}>Confidence</th>
                  </tr></thead>
                  <tbody>
                    {conv.by_campaign.map((c, i) => {
                      const cf = confidence(c.interested || 0, c.contacted || 0)
                      const thin = cf.level !== 'ok'
                      return (
                        // Selecting a campaign row selects its OFFER — that is the
                        // property it shares with the scatter and the offer mix, and
                        // "which campaigns carry the offer I just clicked" is the
                        // question this table answers for them.
                        <tr key={i} {...rowProps({
                          on: hl.on, pick: hl.pick, dim: 'offer', value: c.offer_type,
                          label: c.offer_type, isMatch: selOffer === c.offer_type,
                        })} style={thin ? { opacity: 0.55 } : undefined}>
                          <td className="trunc-1" title={c.campaign_name}>{c.campaign_name}</td>
                          <td><span className="badge trunc-1">{c.offer_type}</span></td>
                          <td>{c.geo}</td>
                          <td className="num">{num(c.contacted)}</td>
                          <td className="num">{num(c.interested)}</td>
                          <td className="num"><b>{pct(c.interested_rate_pct)}</b></td>
                          <td className="num">{pct(c.reply_rate_pct)}</td>
                          <td style={{ fontSize: 11.5, color: thin ? BRAND.amber : BRAND.jade }}>
                            {thin ? `⚠ thin · ${cf.label}` : cf.label}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {cohorts?.cohorts && (
            <>
              <h2 className="section-h">Reply cohorts</h2>
              <p className="muted" style={{ fontSize: 12, marginTop: -4 }}>{cohorts.caveat}</p>
              <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))' }}>
                {Object.entries(cohorts.cohorts).map(([name, c], i) => (
                  <div className="panel" key={name}>
                    <div className="row between">
                      <b>{name}</b>
                      <span className="badge" style={{ color: COLORS[i % COLORS.length], borderColor: COLORS[i % COLORS.length] }}>{c.n} replies</span>
                    </div>
                    {c.personalization_types && (
                      <div style={{ marginTop: 12 }}>
                        <div className="muted" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px', marginBottom: 8 }}>Top personalization</div>
                        {Object.entries(c.personalization_types).sort((a, b) => b[1].count - a[1].count).slice(0, 4).map(([k, v]) => (
                          <div className="row between" key={k} style={{ fontSize: 12.5, marginBottom: 4 }}>
                            <span>{k.replace(/_/g, ' ')}</span>
                            <span className="muted">{v.pct_of_cohort}%</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}
      </>)}
    </div>
  )
}
