import { useEffect, useState } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num, pct } from '../components/ui.jsx'
import { BRAND, SERIES, TOOLTIP_STYLE } from '../theme.js'

// Pillar 5 — Trends: what's working across the interested replies. Reads the
// cached interested-trends analysis; refresh re-runs the fetch + analyze chain.
const COLORS = SERIES

// Horizontal distribution from a {key: {count, pct}} map.
function Dist({ title, data, color = BRAND.jade }) {
  if (!data) return null
  const entries = Object.entries(data).sort((a, b) => (b[1].count || 0) - (a[1].count || 0))
  const max = Math.max(1, ...entries.map(([, v]) => v.count || 0))
  return (
    <div className="panel">
      <div className="section-h" style={{ marginTop: 0 }}>{title}</div>
      {entries.map(([k, v]) => (
        <div key={k} style={{ marginBottom: 9 }}>
          <div className="row between" style={{ fontSize: 12.5, marginBottom: 3 }}>
            <span>{k}</span>
            <span className="muted">{v.count} · {v.pct}%</span>
          </div>
          <div style={{ background: 'var(--panel-2)', borderRadius: 5, height: 8, overflow: 'hidden' }}>
            <div style={{ width: `${(100 * (v.count || 0)) / max}%`, height: '100%', background: color }} />
          </div>
        </div>
      ))}
    </div>
  )
}

export default function TrendsPage() {
  const [data, setData] = useState(null)
  const [variants, setVariants] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)

  function load() {
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
  const when = data?.fetched_at ? new Date(data.fetched_at).toLocaleString() : '—'

  const convChart = (conv?.by_campaign || []).map((c) => ({
    name: c.campaign_name?.slice(0, 16),
    'Interested %': c.interested_rate_pct || 0,
  }))

  return (
    <div>
      <div className="row between">
        <div>
          <h1 className="page-title">Trends — what's working</h1>
          <p className="page-sub">Patterns across interested replies: who replies, to which offer, via which CTA.</p>
        </div>
        <button onClick={refresh} disabled={refreshing}>
          {refreshing ? <Spinner label="Refreshing…" /> : '↻ Refresh analysis'}
        </button>
      </div>

      <div className="banner info">
        Based on <b>{num(data?.total_interested || 0)}</b> interested replies · analysis fetched <b>{when}</b>
      </div>
      <ErrorBanner error={error} />

      {variants?.variants?.length > 0 && (
        <div className="panel" style={{ marginBottom: 22 }}>
          <div className="section-h" style={{ marginTop: 0 }}>By instruction variant (A/B)</div>
          <table>
            <thead><tr><th>Variant</th><th>Contacts</th><th>Enrolled</th><th>Interested</th><th>Interested rate</th></tr></thead>
            <tbody>
              {variants.variants.map((v) => (
                <tr key={v.variant}>
                  <td><span className="badge cta">{v.variant}</span></td>
                  <td>{num(v.total)}</td>
                  <td>{num(v.enrolled)}</td>
                  <td>{num(v.interested)}</td>
                  <td><b>{v.interested_rate_pct == null ? '—' : v.interested_rate_pct + '%'}</b></td>
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

          {convChart.length > 0 && (
            <div className="panel" style={{ marginBottom: 22, height: 300 }}>
              <div className="section-h" style={{ marginTop: 0 }}>Interested rate by campaign (true conversion)</div>
              <ResponsiveContainer width="100%" height="84%">
                <BarChart data={convChart} margin={{ top: 8, right: 16, bottom: 8, left: -8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={BRAND.grid} />
                  <XAxis dataKey="name" tick={{ fill: BRAND.muted, fontSize: 11 }} interval={0} angle={-18} textAnchor="end" height={56} />
                  <YAxis tick={{ fill: BRAND.muted, fontSize: 11 }} unit="%" />
                  <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: 'rgba(15,28,24,0.04)' }} />
                  <Bar dataKey="Interested %" fill={BRAND.jade} radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', marginBottom: 22 }}>
            <Dist title="By seniority" data={s.by_seniority} color={SERIES[0]} />
            <Dist title="By function" data={s.by_function} color={SERIES[1]} />
            <Dist title="Winning CTA type" data={s.by_winning_cta} color={SERIES[3]} />
            <Dist title="Reply intent" data={s.by_reply_intent} color={SERIES[4]} />
            <Dist title="By offer type" data={s.by_offer_type} color={SERIES[5]} />
            <Dist title="Interested via" data={s.by_interested_via} color={SERIES[2]} />
          </div>

          {conv?.by_campaign && (
            <>
              <h2 className="section-h">Conversion by campaign</h2>
              <div className="panel" style={{ padding: 0, marginBottom: 22 }}>
                <table>
                  <thead><tr><th>Campaign</th><th>Offer</th><th>Geo</th><th>Contacted</th><th>Interested</th><th>Interested %</th><th>Reply %</th></tr></thead>
                  <tbody>
                    {conv.by_campaign.map((c, i) => (
                      <tr key={i}>
                        <td>{c.campaign_name}</td>
                        <td><span className="badge">{c.offer_type}</span></td>
                        <td>{c.geo}</td>
                        <td>{num(c.contacted)}</td>
                        <td>{num(c.interested)}</td>
                        <td>{pct(c.interested_rate_pct)}</td>
                        <td>{pct(c.reply_rate_pct)}</td>
                      </tr>
                    ))}
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
    </div>
  )
}
