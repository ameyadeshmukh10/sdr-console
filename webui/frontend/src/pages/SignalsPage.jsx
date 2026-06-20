import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from '../components/ui.jsx'

// Signal cache — per-company research reused for 90 days so a company is searched
// once instead of once per contact / per re-run. Force-refresh re-searches one.
export default function SignalsPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(null)

  function load() {
    api.signals().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])

  async function refresh(domain) {
    setRefreshing(domain); setError(null)
    try {
      const d = await api.refreshSignal(domain)
      if (d.ok === false) setError(d.error || 'refresh failed')
      else setData({ signals: d.signals, count: d.count })
    } catch (e) { setError(e.message) }
    finally { setRefreshing(null) }
  }

  const signals = data?.signals || []
  const fresh = signals.filter((s) => s.fresh).length
  const recent = signals.filter((s) => s.has_recent).length

  return (
    <div>
      <h1 className="page-title">Signals</h1>
      <p className="page-sub">Per-company research cache. Fresh entries (&lt;90 days) are reused, so the AI SDR skips the web search.</p>

      <ErrorBanner error={error} />

      <div className="grid stat-grid" style={{ marginBottom: 20 }}>
        <Stat label="Cached accounts" value={num(data?.count || 0)} />
        <Stat label="Fresh (<90d)" value={num(fresh)} sub="reused, no re-search" />
        <Stat label="Real signal" value={num(recent)} />
        <Stat label="Fallback (no signal)" value={num(signals.length - recent)} sub="product/GTM anchor" />
      </div>

      {!data ? <Spinner label="Loading…" /> : signals.length === 0 ? (
        <div className="empty">No cached signals yet. They populate as you generate batches.</div>
      ) : (
        <div className="panel" style={{ padding: 0 }}>
          <table>
            <thead><tr><th>Domain</th><th>Company</th><th>Type</th><th>Signal</th><th>Age</th><th></th></tr></thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.domain}>
                  <td className="mono">{s.domain}</td>
                  <td>{s.company_name || <span className="muted">—</span>}</td>
                  <td>
                    {s.has_recent
                      ? <span className="badge" style={{ color: 'var(--green)', borderColor: 'var(--green)' }}>recent</span>
                      : <span className="badge" style={{ color: 'var(--amber)', borderColor: 'var(--amber)' }}>fallback</span>}
                  </td>
                  <td className="muted" style={{ maxWidth: 380 }}>{s.signal}</td>
                  <td>
                    <span style={{ color: s.fresh ? 'var(--muted)' : 'var(--red)' }}>
                      {s.age_days == null ? '—' : `${s.age_days}d`}
                    </span>
                  </td>
                  <td>
                    <button className="ghost" disabled={refreshing === s.domain} onClick={() => refresh(s.domain)}>
                      {refreshing === s.domain ? <Spinner /> : '↻ Refresh'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
