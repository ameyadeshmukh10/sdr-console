import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import Addon from './Addon.jsx'

// Wiring signals to the CRM, in both directions.
//
// The CRM is the source of truth. The console computes things — an account signal, a
// priority score, a recommended channel mix — and pushes them into a CRM property so
// they are durable and readable by everyone else. It then reads mapped values BACK
// as authoritative, so a human editing the property in HubSpot wins rather than
// being silently reverted on the next scan.
//
// The mapping is data, not code: a different portal (or Salesforce, where the API
// names differ entirely) is a config change here, not a deploy.

const DIRECTIONS = [
  { id: 'push', label: 'Console → CRM', hint: 'We compute it, the CRM stores it' },
  { id: 'pull', label: 'CRM → Console', hint: 'The CRM owns it, we read it' },
  { id: 'both', label: 'Both', hint: 'We push, but a CRM edit wins on the next read' },
  { id: 'off', label: 'Off', hint: 'Wired but inactive' },
]

export default function CrmFields() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(null)
  const [result, setResult] = useState(null)

  function load() {
    api.crmFields().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])

  async function patch(key, fields) {
    setBusy(key); setError(null)
    try { await api.updateCrmField({ local_key: key, ...fields }); load() }
    catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  async function run(action, dry) {
    setBusy(action); setError(null); setResult(null)
    try { setResult({ action, ...(await api.crmSync({ action, dry_run: dry })) }) }
    catch (e) {
      setError(e.status === 409
        ? 'Demo mode is read-only — CRM sync is disabled while a demo profile is active.'
        : e.message)
    } finally { setBusy(null) }
  }

  const fields = data?.fields || []
  const means = Object.fromEntries((data?.local_fields || []).map((f) => [f.key, f.means]))

  return (
    <div>
      <div className="row" style={{ gap: 8, marginBottom: 2 }}>
        <Addon id="crm-sync" />
      </div>
      <p className="page-sub" style={{ marginTop: 0 }}>
        The CRM is the source of truth. We push what the console computes into these properties,
        and read mapped values back as authoritative — so a change made in HubSpot wins instead
        of being overwritten on the next scan.
      </p>

      <ErrorBanner error={error} />
      {data && data.available === false && (
        <div className="empty">Field mapping is not available for this dataset.</div>
      )}
      {data && data.enabled === false && (
        <div className="banner warn" style={{ marginBottom: 14 }}>
          <b>CRM_SYNC_ENABLED=0</b> — mapping is editable but nothing is read or written.
        </div>
      )}

      <div className="row" style={{ gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <button className="ghost sm" disabled={busy} onClick={() => run('ensure', true)}>
          {busy === 'ensure' ? <Spinner /> : 'Check properties exist'}
        </button>
        <button className="ghost sm" disabled={busy} onClick={() => run('push', true)}>
          {busy === 'push' ? <Spinner /> : 'Preview push'}
        </button>
        <button className="ghost sm" disabled={busy} onClick={() => run('pull', true)}>
          {busy === 'pull' ? <Spinner /> : 'Preview pull'}
        </button>
        <span className="muted" style={{ fontSize: 12, alignSelf: 'center' }}>
          Previews only — nothing is written until you run it for real from the CLI or a sync job.
        </span>
      </div>

      {result && (
        <div className={'banner ' + (result.ok === false ? 'warn' : 'info')} style={{ marginBottom: 14 }}>
          <b>{result.action}</b>{result.dry_run ? ' (preview)' : ''}:{' '}
          {result.ok === false ? (result.error || result.reason) : (
            <>
              {result.action === 'ensure' && (
                <>{num((result.existing || []).length)} already present,{' '}
                  {num((result.created || []).length)} would be created
                  {(result.skipped || []).length > 0 &&
                    `, ${result.skipped.length} skipped (auto-create off)`}</>
              )}
              {result.action === 'push' && (
                <>{num(result.companies)} companies, {num(result.contacts)} contacts
                  {result.no_company ? `, ${num(result.no_company)} unmatched in the CRM` : ''}</>
              )}
              {result.action === 'pull' && (
                <>{num(result.checked)} checked, {num((result.overridden || []).length)} CRM values
                  differ from ours{result.updated ? ` (${num(result.updated)} applied)` : ''}</>
              )}
            </>
          )}
          {(result.errors || []).length > 0 && (
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {result.errors.length} error(s) — first: {String(result.errors[0]).slice(0, 160)}
            </div>
          )}
          {(result.overridden || []).slice(0, 4).map((o, i) => (
            <div key={i} className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {o.domain} · {o.local_key}: CRM says “{o.crm}”
            </div>
          ))}
        </div>
      )}

      {!data ? <Spinner label="Loading…" /> : fields.length > 0 && (
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 1000 }}>
            <thead><tr>
              <th style={{ width: '20%' }}>What we compute</th>
              <th style={{ width: '10%' }}>Object</th>
              <th style={{ width: '22%' }}>CRM property</th>
              <th style={{ width: '17%' }}>Direction</th>
              <th style={{ width: '8%' }}>On</th>
              <th style={{ width: '23%' }}>Last sync</th>
            </tr></thead>
            <tbody>
              {fields.map((f) => (
                <tr key={f.local_key}>
                  <td>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{f.label || f.local_key}</div>
                    <div className="muted" style={{ fontSize: 11 }}>
                      <span className="clamp2">{means[f.local_key] || f.local_key}</span>
                    </div>
                  </td>
                  <td className="muted" style={{ fontSize: 12 }}>{f.object_type}</td>
                  <td>
                    <input defaultValue={f.property_name} className="mono"
                      style={{ width: '100%', fontSize: 12 }}
                      disabled={busy === f.local_key}
                      onBlur={(e) => {
                        if (e.target.value !== f.property_name) {
                          patch(f.local_key, { property_name: e.target.value })
                        }
                      }} />
                  </td>
                  <td>
                    <select value={f.direction} disabled={busy === f.local_key}
                      style={{ width: '100%', fontSize: 12 }}
                      onChange={(e) => patch(f.local_key, { direction: e.target.value })}
                      title={DIRECTIONS.find((d) => d.id === f.direction)?.hint}>
                      {DIRECTIONS.map((d) => (
                        <option key={d.id} value={d.id}>{d.label}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input type="checkbox" checked={!!f.enabled} style={{ width: 'auto' }}
                      disabled={busy === f.local_key}
                      onChange={(e) => patch(f.local_key, { enabled: e.target.checked })} />
                  </td>
                  <td className="muted" style={{ fontSize: 11 }}>
                    {f.last_push_at && <div>pushed {f.last_push_at.slice(0, 10)} ({num(f.pushed)})</div>}
                    {f.last_pull_at && <div>pulled {f.last_pull_at.slice(0, 10)} ({num(f.pulled)})</div>}
                    {!f.last_push_at && !f.last_pull_at && <span>never</span>}
                    {f.last_error && (
                      <div style={{ color: 'var(--red)' }} title={f.last_error}>
                        <span className="clamp2">{f.last_error}</span>
                      </div>
                    )}
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
