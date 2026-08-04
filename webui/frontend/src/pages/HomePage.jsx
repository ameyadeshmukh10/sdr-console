import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from 'recharts'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from '../components/ui.jsx'
import { BRAND, PERSONA_COLORS, TOOLTIP_STYLE } from '../theme.js'
import { BAND_COLOR } from '../components/campaignShared.jsx'
import ContactLink, { Phone } from '../components/ContactLink.jsx'
import Addon from '../components/Addon.jsx'

// Home — outcome first, then what needs a human, then whether it's improving.
//
// Two rules that shape everything here:
//   1. Every widget owns exactly one destination. No link, no place on Home.
//   2. Widgets are MINIATURES of the page they link to — the same figures, the same
//      visual language. No generated prose summarising numbers that are right there;
//      a sentence like "worth acting on: 7.7x better" reads as filler next to real
//      data and ages badly.
// Cards in a row share a height (see .home-grid / .home-widget in styles.css), so
// the page reads as a grid rather than ragged columns.

const usd = (v) => (v == null ? '—' : `$${Number(v).toLocaleString()}`)

// Interested rates live under 1%, so a shared formatter keeps them readable and
// consistent: 2 decimals below 1%, 1 above. Raw values render as 0.059% otherwise.
const rate = (v) => {
  if (v == null) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return `${n === 0 ? 0 : n < 1 ? Number(n.toFixed(2)) : Number(n.toFixed(1))}%`
}

function Widget({ title, to, linkLabel, children, wide = false }) {
  return (
    <section className={'panel home-widget' + (wide ? ' wide' : '')}>
      <header className="row between home-widget-h">
        <div className="section-h" style={{ marginTop: 0, marginBottom: 0 }}>{title}</div>
        {to && <Link to={to} className="home-more">{linkLabel || 'View'} →</Link>}
      </header>
      <div className="home-widget-body">{children}</div>
    </section>
  )
}

function Delta({ value }) {
  if (value == null || value === 0) return null
  const up = value > 0
  return (
    <span className="home-delta" style={{ color: up ? BRAND.jade : BRAND.red }}>
      {up ? '↑' : '↓'} {num(Math.abs(value))} this week
    </span>
  )
}

function OutcomeTile({ label, value, sub, delta, accent, to }) {
  return (
    <Link to={to} className={'stat home-tile' + (accent ? ' accent' : '')}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      <div className="sub">
        {sub}{sub && delta ? ' · ' : ''}<Delta value={delta} />
      </div>
    </Link>
  )
}

const INTENT_LABEL = {
  meeting_request: 'meeting', info_request: 'info', pricing: 'pricing',
  referral: 'referral', positive_later: 'later', positive_other: 'positive',
  not_interested: 'not interested',
}

function agoLabel(iso) {
  if (!iso) return ''
  const h = Math.round((Date.now() - new Date(iso).getTime()) / 3.6e6)
  if (!Number.isFinite(h)) return ''
  if (h < 1) return 'just now'
  if (h < 24) return `${h}h ago`
  return `${Math.round(h / 24)}d ago`
}

// Mirrors the Pipeline view's status split.
function Funnel({ counts, total }) {
  const ORDER = [
    ['pending', 'Pending', 'rgba(15,28,24,0.14)'],
    ['generated', 'Generated', BRAND.mint],
    ['enrolled', 'Enrolled', BRAND.jade],
    ['skipped', 'Skipped', BRAND.amber],
  ]
  const rows = ORDER.filter(([k]) => (counts[k] || 0) > 0)
  const sum = rows.reduce((a, [k]) => a + (counts[k] || 0), 0) || 1
  return (
    <>
      <div className="home-funnel">
        {rows.map(([k, , color]) => (
          <div key={k} style={{ width: `${(100 * counts[k]) / sum}%`, background: color }}
            title={`${k}: ${counts[k]}`} />
        ))}
      </div>
      <dl className="home-legend">
        {rows.map(([k, label, color]) => (
          <div key={k}>
            <dt><span className="dot" style={{ background: color }} />{label}</dt>
            <dd>{num(counts[k])}</dd>
          </div>
        ))}
      </dl>
      <div className="home-note">{num(total)} contacts total</div>
    </>
  )
}

// Mirrors the Trends sequence chart: reach per step with the rate beside it.
function StepTable({ steps }) {
  if (!steps?.length) return null
  const max = Math.max(...steps.map((s) => s.rate || 0), 0.0001)
  return (
    <table className="home-steps">
      <tbody>
        {steps.map((s) => (
          <tr key={s.step}>
            <th>Step {s.step}</th>
            <td className="muted">{num(s.contacted)} sent</td>
            <td className="home-steps-bar">
              <span style={{ width: `${Math.max(3, (100 * (s.rate || 0)) / max)}%` }} />
            </td>
            <td className="home-steps-rate">{rate(s.rate)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function HomePage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.home().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }, [])

  if (error || !data) {
    return (
      <div>
        <h1 className="page-title">Home</h1>
        {error ? <ErrorBanner error={error} /> : <Spinner label="Loading…" />}
      </div>
    )
  }

  const { outcome, queue, trend, pipeline, attention, campaigns, reviews } = data.sections
  const sig = queue?.signals
  const alerts = attention?.alerts || []

  const tasks = []
  if (queue) {
    if (queue.replies_waiting > 0) {
      tasks.push({ to: '/replies', cta: 'Review', count: queue.replies_waiting,
        label: `interested ${queue.replies_waiting === 1 ? 'reply' : 'replies'} to work` })
    }
    if (queue.generated_ready > 0) {
      tasks.push({ to: '/pipeline', cta: 'Enroll', count: queue.generated_ready,
        label: 'contacts with copy ready to enroll' })
    }
    if (queue.pending_batches > 0) {
      tasks.push({ to: '/pipeline', cta: 'Generate', count: queue.pending_batches,
        label: `${queue.pending_batches === 1 ? 'batch' : 'batches'} awaiting copy generation` })
    }
    if (queue.signals_missing > 0) {
      tasks.push({ to: '/signals', cta: 'Detect', count: queue.signals_missing,
        label: 'accounts missing a signal scan' })
    }
  }

  const dir = trend?.latest != null && trend?.previous != null
    ? trend.latest - trend.previous : null

  return (
    <div>
      <h1 className="page-title">Home</h1>

      {alerts.length > 0 && (
        <div style={{ marginBottom: 20 }}>
          {alerts.map((a, i) => (
            <Link key={i} to={a.link}
              className={'banner ' + (a.level === 'error' ? 'warn' : 'info')}
              style={{ display: 'block', marginBottom: 8, textDecoration: 'none' }}>
              <b>{a.level === 'error' ? '⚠ ' : ''}Attention:</b> {a.text}
            </Link>
          ))}
        </div>
      )}

      {/* Evergreen cycles waiting on a decision. Above the numbers because an
          evergreen campaign in review is SILENTLY NOT SENDING — it is the one
          campaign state where nothing at all happens until a human acts, and a
          queue nobody is shown is a queue nobody works. Absent when empty. */}
      {(reviews?.reviews || []).length > 0 && (
        <div className="review-strip">
          <div className="review-strip-h">
            {reviews.reviews.length === 1
              ? 'A campaign finished its cycle and is waiting on you'
              : `${reviews.reviews.length} campaigns finished their cycle and are waiting on you`}
          </div>
          {reviews.reviews.map((r) => (
            <Link key={r.campaign_id} to="/campaigns" className="review-row">
              <span className="review-name">{r.name}</span>
              <span className="muted">cycle {r.cycle}</span>
              <span className="muted">
                {num(r.accounts)} accounts · {num(r.enrolled)} enrolled
                {r.replied ? ` · ${num(r.replied)} replied` : ''}
              </span>
              <span className="review-cta">Review &amp; relaunch →</span>
            </Link>
          ))}
        </div>
      )}

      {/* ---- what the worker produced ------------------------------------ */}
      <div className="grid stat-grid" style={{ marginBottom: 20 }}>
        <OutcomeTile label="Pipeline attributed" accent to="/analytics"
          value={usd(outcome?.pipeline_attributed)}
          sub={outcome?.deals_created != null
            ? `${num(outcome.deals_created)} deals` : 'not synced'} />
        <OutcomeTile label="Interested replies" to="/replies"
          value={num(outcome?.interested)}
          sub={outcome?.interested_rate_pct != null
            ? `${rate(outcome.interested_rate_pct)} of replies` : null}
          delta={outcome?.deltas?.interested} />
        <OutcomeTile label="Replies" to="/analytics"
          value={num(outcome?.replies)}
          sub={outcome?.reply_rate_pct != null
            ? `${rate(outcome.reply_rate_pct)} reply rate` : null}
          delta={outcome?.deltas?.replies} />
        <OutcomeTile label="Contacts worked" to="/outreach"
          value={num(outcome?.contacted)}
          sub={outcome?.leads != null ? `of ${num(outcome.leads)}` : null}
          delta={outcome?.deltas?.contacted} />
      </div>

      <div className="grid home-grid">
        {/* ---- what needs a human --------------------------------------- */}
        <Widget title="Needs you now" to="/replies" linkLabel="Replies">
          {tasks.length === 0 ? (
            <p className="home-clear">Queue is clear — no replies, copy or batches waiting.</p>
          ) : (
            <ul className="home-tasks">
              {tasks.map((t, i) => (
                <li key={i}>
                  <Link to={t.to}>
                    <b>{num(t.count)}</b>
                    <span className="home-task-label">{t.label}</span>
                    <span className="home-task-cta">{t.cta} →</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}

          {queue?.replies_top?.length > 0 && (
            <table className="home-replies">
              <tbody>
                {queue.replies_top.map((r) => (
                  <tr key={r.reply_id}>
                    <th>{r.from_name}</th>
                    <td className="muted home-reply-co">{r.company}</td>
                    <td>{r.intent && (
                      <span className="badge cta">{INTENT_LABEL[r.intent] || r.intent}</span>
                    )}</td>
                    <td className="muted home-reply-ago">{agoLabel(r.date_received)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Widget>

        {/* ---- is it improving ------------------------------------------ */}
        <Widget title={trend?.available ? 'Conversion — latest window' : 'Conversion — to date'}
          to="/trends" linkLabel="Trends">
          <div className="row between" style={{ alignItems: 'flex-start', gap: 14 }}>
            <div>
              <div className="home-big">
                {rate(trend?.available ? trend.latest : trend?.overall_rate)}
              </div>
              <div className="home-note">
                {trend?.available ? (
                  <>vs previous window
                    {dir != null && (
                      <span style={{ color: dir >= 0 ? BRAND.jade : BRAND.red, fontWeight: 600 }}>
                        {' '}{dir >= 0 ? '+' : ''}{dir.toFixed(2)} pts
                      </span>
                    )}
                  </>
                ) : (
                  <>interested rate to date · {num(trend?.total_interested)} replies</>
                )}
              </div>
            </div>
            {trend?.available && (
              <div className="home-spark">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={trend.points} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
                    <YAxis hide domain={['dataMin', 'dataMax']} />
                    <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v) => rate(v)} />
                    <Area type="monotone" dataKey="rate" stroke={BRAND.jade}
                      strokeWidth={2} fill="rgba(34,130,111,0.13)" dot={false} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
          <div className="home-sub-h">By sequence step</div>
          <StepTable steps={trend?.steps} />
        </Widget>

        {/* ---- who to work next ----------------------------------------- */}
        {/* A miniature of the call list, not a campaign dashboard: "who do I call
            next" is the only campaign question worth putting on Home. Everything
            else about a campaign belongs on the campaign. */}
        <Widget title="Work next" to="/calllist" linkLabel="Call list">
          {!campaigns ? (
            <p className="home-clear">
              No campaigns yet — define one from Use to build a prioritised call list.
            </p>
          ) : campaigns.call_list.length === 0 ? (
            <p className="home-clear">
              {campaigns.active > 0
                ? `${num(campaigns.active)} active campaign${campaigns.active === 1 ? '' : 's'}, nothing waiting to be worked.`
                : 'No accounts qualified yet — run Find accounts on a campaign to scan for signal.'}
            </p>
          ) : (
            <>
              <ul className="home-signals">
                {campaigns.call_list.map((m) => (
                  <li key={m.contact_id}>
                    <Link to={`/outreach?q=${encodeURIComponent(m.company || '')}`}>
                      <div className="home-sig-head">
                        <span className="home-sig-co">
                          <span className="badge" style={{
                            marginRight: 6,
                            color: BAND_COLOR[m.band] || BRAND.muted,
                            borderColor: BAND_COLOR[m.band] || BRAND.muted,
                          }}>{Math.round(m.score ?? 0)}</span>
                          {m.name || m.company}
                        </span>
                        <span className="home-sig-meta">{m.company}</span>
                      </div>
                      <div className="home-sig-text">{m.signal || 'no recorded signal'}</div>
                    </Link>
                  </li>
                ))}
              </ul>
              <div className="home-note">
                {num(campaigns.waiting)} waiting across {num(campaigns.active)} active
                campaign{campaigns.active === 1 ? '' : 's'}
                {campaigns.bands?.hot ? ` · ${num(campaigns.bands.hot)} hot` : ''}
              </div>
            </>
          )}
        </Widget>

        {/* ---- pipeline state ------------------------------------------- */}
        <Widget title="Pipeline" to="/pipeline" linkLabel="Pipeline">
          {pipeline?.total_contacts ? (
            <>
              <Funnel counts={pipeline.contacts_by_status} total={pipeline.total_contacts} />
              <div className="home-sub-h">By persona</div>
              <dl className="home-legend">
                {Object.entries(pipeline.by_persona || {})
                  .sort((a, b) => b[1] - a[1])
                  .map(([p, n]) => (
                    <div key={p}>
                      <dt>
                        <span className="dot round"
                          style={{ background: PERSONA_COLORS[p] || BRAND.muted }} />
                        {p}
                      </dt>
                      <dd>{num(n)}</dd>
                    </div>
                  ))}
              </dl>
              <div className="home-note">
                {pipeline.active_batches > 0
                  ? `${pipeline.active_batches} of ${num(Object.values(pipeline.batches_by_status || {}).reduce((a, b) => a + b, 0))} batches still active`
                  : `All ${num(Object.values(pipeline.batches_by_status || {}).reduce((a, b) => a + b, 0))} batches complete`}
              </div>
            </>
          ) : (
            <p className="home-clear">No contacts yet — pull a HubSpot list from Use to start.</p>
          )}
        </Widget>

        {/* ---- what the signals just found ------------------------------ */}
        {/* Full width and last: the drill-down needs room, and "what just fired,
            and who do I call about it" is the note you want to leave the page on.
            A signal you can't act on from where you read it is a notification. */}
        <Widget title="Recent signals" to="/signals" linkLabel="All signals" wide>
          {!sig?.recent?.length ? (
            <p className="home-clear">
              No accounts researched yet — signals are gathered the first time a batch
              generates copy for a company.
            </p>
          ) : (
            <>
              <ul className="home-signals">
                {sig.recent.map((r) => (
                  <li key={r.domain}>
                    {/* Straight to that account's contacts, which is the question a
                        signal immediately raises: who do we have there? */}
                    <Link to={`/outreach?company=${encodeURIComponent(r.company)}`}>
                      <div className="home-sig-head">
                        <span className="home-sig-co">{r.company}</span>
                        <span className="home-sig-meta">
                          {r.contacts > 0 && `${num(r.contacts)} contact${r.contacts === 1 ? '' : 's'}`}
                          {r.checked_at && ` · ${agoLabel(r.checked_at)}`}
                        </span>
                      </div>
                      <div className="home-sig-text">{r.signal}</div>
                      {(r.tech || r.hiring) && (
                        <div className="home-sig-tags">
                          {r.hiring && <span className="badge">{r.hiring.split(':')[0].split('·').pop().trim()} hiring</span>}
                          {r.tech && <span className="badge">{r.tech.split('·')[0].trim()}</span>}
                        </div>
                      )}
                    </Link>
                    {(r.people || []).length > 0 && (
                      <div className="home-sig-contacts">
                        {r.people.map((p) => (
                          <div className="home-sig-row" key={p.contact_id}>
                            <span className="who">
                              <ContactLink contact={p} showTitle />
                            </span>
                            <span className="row" style={{ gap: 8 }}>
                              {p.priority_score != null && (
                                <span className="badge" style={{
                                  color: BAND_COLOR[p.score_band] || BRAND.muted,
                                  borderColor: BAND_COLOR[p.score_band] || BRAND.muted,
                                }}>{Math.round(p.priority_score)}</span>
                              )}
                              <Phone contact={p} />
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
              <div className="home-note">
                {num(sig.hiring_hook)} of {num(sig.total)} accounts have a sales-hiring
                hook · {num(sig.no_hook)} have none
              </div>
            </>
          )}
        </Widget>
      </div>
    </div>
  )
}
