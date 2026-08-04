import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  BarChart, Bar, Cell, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from 'recharts'
import { api } from '../api.js'
import CampaignAnalytics from '../components/CampaignAnalytics.jsx'
import Funnel from '../components/Funnel.jsx'
import Reports from '../components/Reports.jsx'
import AttributionOutcome from '../components/AttributionOutcome.jsx'
import { useHighlight, SelectionBar, rowProps } from '../components/crossHighlight.jsx'
import { Stat, Spinner, ErrorBanner, num, pct, EmailIcon, LinkedInIcon } from '../components/ui.jsx'
import { BRAND, TOOLTIP_STYLE } from '../theme.js'

// Pillar 3 — Analytics: campaign performance from cached stats, refreshable live.

// One timestamp format for the page. toLocaleString() prints seconds, which is
// noise on a nightly job and made the two sync stamps on this screen read as if
// they disagreed about precision.
const when = (v) => (v ? new Date(v).toLocaleString(undefined, {
  day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
}) : '—')

const TABS = [
  { id: 'outcome', label: 'Outcome' },
  { id: 'funnel', label: 'Funnel' },
  { id: 'channels', label: 'Channels' },
  { id: 'data', label: 'Raw data' },
]

export default function AnalyticsPage() {
  // Deep link from Use: /analytics?tab=funnel&campaign=<id>
  const [params] = useSearchParams()
  const [tab, setTab] = useState(() => {
    const t = params.get('tab')
    return TABS.some((x) => x.id === t) ? t : 'outcome'
  })
  const focusCampaign = Number(params.get('campaign')) || null
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [li, setLi] = useState(null)   // LinkedIn analytics
  const [aisdr, setAisdr] = useState(null)  // AI SDR deal attribution (MongoDB)
  const [syncMsg, setSyncMsg] = useState(null)
  const [syncBusy, setSyncBusy] = useState(false)

  function load() {
    api.analytics().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }
  function loadAisdr() {
    api.aisdrAnalytics().then(setAisdr).catch(() => setAisdr({ configured: true, error: 'unreachable' }))
  }
  useEffect(() => { load() }, [])
  useEffect(() => { api.linkedinAnalytics().then(setLi).catch(() => setLi({ error: 'unreachable' })) }, [])
  useEffect(() => { loadAisdr() }, [])

  // Kick the HubSpot -> MongoDB attribution sync, then poll until it finishes
  // (the seed run takes a couple of minutes) and refresh the tiles.
  async function syncAisdr() {
    setSyncBusy(true)
    setSyncMsg('Starting attribution sync…')
    try {
      await api.aisdrSync()
    } catch (e) {
      // 409 = a sync is already running (e.g. the nightly job) — keep polling it.
      if (e.message !== '409') {
        setSyncMsg(`Attribution sync failed to start: ${e.message}`)
        setSyncBusy(false)
        return
      }
    }
    setSyncMsg('Attribution sync running — pulling emails, deals and contacts from HubSpot…')
    for (let i = 0; i < 90; i++) {           // up to ~7.5 min
      await new Promise((r) => setTimeout(r, 5000))
      try {
        const s = await api.aisdrSyncStatus()
        if (!s.running) {
          setSyncMsg(s.last_run_ok === false ? `Attribution sync finished with an error: ${s.last_error || 'unknown'}` : 'Attribution sync complete.')
          setSyncBusy(false)
          loadAisdr()
          return
        }
      } catch { /* transient — keep polling */ }
    }
    setSyncMsg('Attribution sync is still running — refresh the page later.')
    setSyncBusy(false)
  }

  async function refresh() {
    setRefreshing(true); setError(null)
    try {
      const d = await api.refreshAnalytics()
      setData(d)
      if (!d.ok) setError('Refresh script returned errors — showing latest cached data.')
    } catch (e) { setError(e.message) }
    finally { setRefreshing(false) }
  }

  // One button, both jobs: the email-stats snapshot refresh and the deal
  // attribution sync run concurrently.
  async function refreshAll() {
    const jobs = [refresh()]
    if (aisdr?.configured !== false && !syncBusy) jobs.push(syncAisdr())
    await Promise.allSettled(jobs)
  }

  const t = data?.totals
  // Only campaigns that actually sent are worth charting.
  const active = (data?.campaigns || []).filter((c) => (c.total_leads_contacted || 0) > 0)
  const chartData = active.map((c) => ({
    id: c.campaign_id,
    name: c.campaign_name?.slice(0, 18) || `#${c.campaign_id}`,
    'Reply %': c.reply_rate_pct || 0,
    'Interested %': c.interested_rate_pct || 0,
  }))

  // Channels tab: the chart and the table below it are the same campaigns, so a
  // click in either marks the other. Without it, finding the bar for a given row
  // means counting along the axis.
  const chan = useHighlight()
  const chanSel = chan.sel?.dim === 'campaign' ? chan.sel.value : null
  const chanRow = (data?.campaigns || []).find((c) => c.campaign_id === chanSel)
  // Funnel tab: one selection shared by the funnel and the campaign table.
  const fun = useHighlight()

  const fetchedWhen = when(data?.fetched_at)

  return (
    <div>
      <div className="row between">
        <h1 className="page-title">Analytics</h1>
        <button onClick={refreshAll} disabled={refreshing || syncBusy}>
          {refreshing || syncBusy ? <Spinner label="Refreshing…" /> : '↻ Refresh'}
        </button>
      </div>

      {/* Named for WHICH sync it is: the deal-attribution stamp lives on the Total
          pipeline tile, and two unlabelled "last synced" times on one screen read
          as a contradiction rather than as two different jobs. */}
      <div className="banner info">Email stats synced <b>{fetchedWhen}</b></div>
      <ErrorBanner error={error} />

      {/* Three tabs following the system's causal chain: what it produced, where the
          chain leaks, and how each channel performed. The Bison campaign used to be
          the unit of analysis, which could only ever see the last third of that. */}
      <nav className="uth-tabs" style={{ marginBottom: 22 }}>
        {TABS.map((t) => (
          <button key={t.id} type="button"
            className={'uth-tab' + (tab === t.id ? ' active' : '')}
            onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </nav>

      {/* AI SDR deal attribution — nightly HubSpot -> MongoDB sync. Its four
          widgets are cross-linked cuts of one deal set (see AttributionOutcome). */}
      {tab === 'outcome' && <AttributionOutcome aisdr={aisdr} syncMsg={syncMsg} />}

      {tab === 'data' && <Reports />}

      {/* One selection across both tables: a campaign is a row in the funnel AND a
          row in the campaign table, and a funnel stage is the column those rows sum
          into. Sharing the hook is what makes picking either one mark the other. */}
      {tab === 'funnel' && (<>
        <SelectionBar sel={fun.sel} clear={fun.clear}
          summary={fun.sel?.dim === 'stage'
            ? 'shown per campaign in the table below'
            : 'marked in both tables'}
          hint="Click a funnel stage to break it out per campaign, or a campaign to follow it through both tables." />
        <Funnel focusCampaign={focusCampaign} hl={fun} />
        <CampaignAnalytics hl={fun} />
      </>)}

      {tab === 'channels' && (<>
      {/* Email channel */}
      <div className="row" style={{ gap: 8, alignItems: 'center', marginBottom: 10 }}>
        <svg width="18" height="14"><EmailIcon x={1} y={1} color={BRAND.jade} /></svg>
        <h2 className="section-h" style={{ margin: 0 }}>Email</h2>
      </div>
      {!data ? <Spinner label="Loading…" /> : (
        <div className="grid stat-grid" style={{ marginBottom: 24 }}>
          <Stat label="Total leads" value={num(t.total_leads)} />
          <Stat label="Contacted" value={num(t.total_contacted)} />
          <Stat label="Replies" value={num(t.total_replies)} sub={`${pct(t.overall_reply_rate_pct)} reply rate`} />
          <Stat label="Interested" value={num(t.total_interested)} sub={`${pct(t.overall_interested_rate_pct)} interested rate`} accent />
        </div>
      )}

      {/* LinkedIn channel */}
      <div className="row" style={{ gap: 8, alignItems: 'center' }}>
        <svg width="18" height="18"><LinkedInIcon x={1} y={1} /></svg>
        <h2 className="section-h" style={{ margin: 0 }}>LinkedIn</h2>
      </div>
      {!li ? <Spinner label="Loading LinkedIn…" />
        : li.configured === false ? (
          <div className="banner info" style={{ marginTop: 10 }}>LinkedIn analytics not configured.</div>
        ) : li.error ? (
          <div className="banner warn" style={{ marginTop: 10 }}>Couldn't load LinkedIn stats: {li.error}</div>
        ) : (() => {
          const s = li.stats || {}, f = li.funnel || {}
          const rate = (n, d) => (d ? (100 * n) / d : 0)
          const liActive = (f.totalUsersInProgress || 0) + (f.totalUsersPending || 0)
          return (
            <>
              <div className="banner info" style={{ marginTop: 10 }}>
                Campaign <b>{li.campaign_name || `#${li.campaign_id}`}</b> · <span className="badge">{li.status}</span> · <span className="mono">#{li.campaign_id}</span>
              </div>
              <div className="grid stat-grid" style={{ marginTop: 12 }}>
                <Stat label="Leads in campaign" value={num(f.totalUsers)} sub={`${num(f.totalUsersFinished)} finished · ${num(liActive)} active`} />
                <Stat label="Connections sent" value={num(s.connectionsSent)} sub={`${num(s.connectionsAccepted)} accepted · ${pct(rate(s.connectionsAccepted, s.connectionsSent))}`} />
                <Stat label="Messages sent" value={num(s.messagesSent)} sub={`${num(s.totalMessageReplies)} replies · ${pct(rate(s.totalMessageReplies, s.messagesSent))}`} />
                <Stat label="Interested (auto-tagged)" value={num(s.autoTaggedInterested)} sub={`${pct(rate(s.autoTaggedInterested, s.uniqueLeadsContacted))} of contacted`} accent />
              </div>
              {(s.connectionsSent || 0) === 0 && (
                <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
                  No LinkedIn activity yet — metrics populate once the campaign starts sending. (LinkedIn
                  has no email-style "reply rate" feed; these are native connection/message stats.)
                </p>
              )}
            </>
          )
        })()}

      {data && (
        <>
          <div style={{ marginTop: 30 }}>
            <SelectionBar
              sel={chan.sel} clear={chan.clear}
              summary={chanRow && `${num(chanRow.total_leads_contacted)} contacted · `
                + `${pct(chanRow.reply_rate_pct)} reply · ${pct(chanRow.interested_rate_pct)} interested`}
              hint={chartData.length > 0
                ? 'Click a bar or a campaign row to line the two up.' : null} />
          </div>

          {chartData.length > 0 && (
            <div className="panel" style={{ marginBottom: 24, height: 320 }}>
              <div className="section-h" style={{ marginTop: 0 }}>Reply vs interested rate by email campaign</div>
              <ResponsiveContainer width="100%" height="86%">
                <BarChart data={chartData} margin={{ top: 8, right: 16, bottom: 8, left: -8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={BRAND.grid} />
                  <XAxis dataKey="name" tick={{ fill: BRAND.muted, fontSize: 11 }} interval={0} angle={-18} textAnchor="end" height={60} />
                  <YAxis tick={{ fill: BRAND.muted, fontSize: 11 }} unit="%" />
                  <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: 'rgba(15,28,24,0.04)' }} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  {/* Per-bar opacity rather than a colour swap: the two series have to
                      stay distinguishable from each other while one campaign is
                      picked out from the rest. */}
                  {['Reply %', 'Interested %'].map((k) => (
                    <Bar key={k} dataKey={k} fill={k === 'Reply %' ? BRAND.jade : BRAND.mint}
                      radius={[3, 3, 0, 0]} isAnimationActive={false}
                      onClick={(d) => chan.pick('campaign', d.id, d.name)}
                      cursor="pointer">
                      {chartData.map((d) => (
                        <Cell key={d.id}
                          fillOpacity={!chanSel || chanSel === d.id ? 1 : 0.22} />
                      ))}
                    </Bar>
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          <h2 className="section-h" style={chartData.length > 0 ? undefined : { marginTop: 0 }}>Email campaigns</h2>
          <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
            <table style={{ tableLayout: 'fixed', width: '100%', minWidth: 860 }}>
              <thead>
                <tr>
                  <th style={{ width: '24%' }}>Campaign</th>
                  <th style={{ width: '10%' }}>Status</th>
                  <th style={{ width: '10%' }} className="num">Leads</th>
                  <th style={{ width: '11%' }} className="num">Contacted</th>
                  <th style={{ width: '10%' }} className="num">Replies</th>
                  <th style={{ width: '10%' }} className="num">Reply %</th>
                  <th style={{ width: '11%' }} className="num">Interested</th>
                  <th style={{ width: '14%' }} className="num">Interested %</th>
                </tr>
              </thead>
              <tbody>
                {data.campaigns.map((c) => (
                  <tr key={c.campaign_id} {...rowProps({
                    on: chan.on, pick: chan.pick, dim: 'campaign', value: c.campaign_id,
                    label: c.campaign_name || `#${c.campaign_id}`,
                    isMatch: chanSel === c.campaign_id,
                  })}>
                    <td className="trunc-1" title={c.campaign_name || ''}>
                      {c.campaign_name || `#${c.campaign_id}`}</td>
                    <td><span className="badge">{c.status}</span></td>
                    <td className="num">{num(c.total_leads)}</td>
                    <td className="num">{num(c.total_leads_contacted)}</td>
                    <td className="num">{num(c.unique_replies)}</td>
                    <td className="num">{pct(c.reply_rate_pct)}</td>
                    <td className="num">{num(c.interested)}</td>
                    <td className="num">{pct(c.interested_rate_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {data.errors?.length > 0 && (
            <div className="banner warn" style={{ marginTop: 16 }}>
              {data.errors.length} campaign(s) returned errors during the last fetch (e.g. drafts without a sequence).
            </div>
          )}
        </>
      )}
      </>)}
    </div>
  )
}
