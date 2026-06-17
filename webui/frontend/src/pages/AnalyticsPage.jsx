import { useEffect, useState } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from 'recharts'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num, pct } from '../components/ui.jsx'

// Pillar 3 — Analytics: campaign performance from cached stats, refreshable live.
export default function AnalyticsPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)

  function load() {
    api.analytics().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])

  async function refresh() {
    setRefreshing(true); setError(null)
    try {
      const d = await api.refreshAnalytics()
      setData(d)
      if (!d.ok) setError('Refresh script returned errors — showing latest cached data.')
    } catch (e) { setError(e.message) }
    finally { setRefreshing(false) }
  }

  const t = data?.totals
  // Only campaigns that actually sent are worth charting.
  const active = (data?.campaigns || []).filter((c) => (c.total_leads_contacted || 0) > 0)
  const chartData = active.map((c) => ({
    name: c.campaign_name?.slice(0, 18) || `#${c.campaign_id}`,
    'Reply %': c.reply_rate_pct || 0,
    'Interested %': c.interested_rate_pct || 0,
  }))

  const fetchedWhen = data?.fetched_at ? new Date(data.fetched_at).toLocaleString() : '—'

  return (
    <div>
      <div className="row between">
        <div>
          <h1 className="page-title">Analytics</h1>
          <p className="page-sub">Email Bison campaign performance. Cached snapshot — refresh to pull live.</p>
        </div>
        <button onClick={refresh} disabled={refreshing}>
          {refreshing ? <Spinner label="Refreshing…" /> : '↻ Refresh from Bison'}
        </button>
      </div>

      <div className="banner info">Snapshot fetched: <b>{fetchedWhen}</b></div>
      <ErrorBanner error={error} />

      {!data ? <Spinner label="Loading…" /> : (
        <>
          <div className="grid stat-grid" style={{ marginBottom: 24 }}>
            <Stat label="Total leads" value={num(t.total_leads)} />
            <Stat label="Contacted" value={num(t.total_contacted)} />
            <Stat label="Replies" value={num(t.total_replies)} sub={`${pct(t.overall_reply_rate_pct)} reply rate`} />
            <Stat label="Interested" value={num(t.total_interested)} sub={`${pct(t.overall_interested_rate_pct)} interested rate`} />
          </div>

          {chartData.length > 0 && (
            <div className="panel" style={{ marginBottom: 24, height: 320 }}>
              <div className="section-h" style={{ marginTop: 0 }}>Reply vs interested rate by campaign</div>
              <ResponsiveContainer width="100%" height="86%">
                <BarChart data={chartData} margin={{ top: 8, right: 16, bottom: 8, left: -8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#2a3340" />
                  <XAxis dataKey="name" tick={{ fill: '#8b97a6', fontSize: 11 }} interval={0} angle={-18} textAnchor="end" height={60} />
                  <YAxis tick={{ fill: '#8b97a6', fontSize: 11 }} unit="%" />
                  <Tooltip contentStyle={{ background: '#161b22', border: '1px solid #2a3340', borderRadius: 8 }} />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="Reply %" fill="#4f9dff" radius={[3, 3, 0, 0]} />
                  <Bar dataKey="Interested %" fill="#3fb950" radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          <h2 className="section-h">Campaigns</h2>
          <div className="panel" style={{ padding: 0 }}>
            <table>
              <thead>
                <tr><th>Campaign</th><th>Status</th><th>Leads</th><th>Contacted</th><th>Replies</th><th>Reply %</th><th>Interested</th><th>Interested %</th></tr>
              </thead>
              <tbody>
                {data.campaigns.map((c) => (
                  <tr key={c.campaign_id}>
                    <td>{c.campaign_name || `#${c.campaign_id}`}</td>
                    <td><span className="badge">{c.status}</span></td>
                    <td>{num(c.total_leads)}</td>
                    <td>{num(c.total_leads_contacted)}</td>
                    <td>{num(c.unique_replies)}</td>
                    <td>{pct(c.reply_rate_pct)}</td>
                    <td>{num(c.interested)}</td>
                    <td>{pct(c.interested_rate_pct)}</td>
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
    </div>
  )
}
