import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from '../components/ui.jsx'
import ListPicker from '../components/ListPicker.jsx'
import SourcePanel from '../components/SourcePanel.jsx'
import CampaignsPanel from '../components/CampaignsPanel.jsx'
import CallList from '../components/CallList.jsx'
import HotTargets from '../components/HotTargets.jsx'
import CapacityPanel from '../components/CapacityPanel.jsx'
import CrmFields from '../components/CrmFields.jsx'
import BuyerGroup from '../components/BuyerGroup.jsx'

// Pillar 1 — Use: put the AI SDR to work. Three tabs, in the order the work
// actually happens:
//
//   Campaigns  define who to work and what each touch offers
//   Call list  the priority-ordered result, strongest signal first
//   Source     top the contact pool up from a HubSpot list
//
// Campaigns lead because starting one IS how you put the worker to work; sourcing
// is the supply step behind it. Merging them here rather than giving campaigns their
// own nav item keeps "how do I start" a single destination.
const TABS = [
  { id: 'campaigns', label: 'Campaigns' },
  { id: 'hot', label: 'Hot targets' },
  { id: 'calllist', label: 'Call list' },
  { id: 'source', label: 'Source contacts' },
  { id: 'capacity', label: 'Capacity & spend' },
  { id: 'buyers', label: 'Buyer group' },
  { id: 'crm', label: 'CRM fields' },
]

export default function UsePage({ initialTab = 'campaigns' }) {
  const [tab, setTab] = useState(initialTab)
  return (
    <div>
      <h1 className="page-title">Use — put the AI SDR to work</h1>

      <nav className="uth-tabs" style={{ marginBottom: 22 }}>
        {TABS.map((t) => (
          <button key={t.id} type="button"
            className={'uth-tab' + (tab === t.id ? ' active' : '')}
            onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </nav>

      {tab === 'campaigns' && <CampaignsPanel />}
      {tab === 'hot' && <HotTargets />}
      {tab === 'calllist' && <CallList />}
      {tab === 'source' && <SourceTab />}
      {tab === 'capacity' && <CapacityPanel />}
      {tab === 'buyers' && <BuyerGroup />}
      {tab === 'crm' && <CrmFields />}
    </div>
  )
}

// The original Use view: pull a HubSpot list into the pipeline and batch it.
function SourceTab() {
  const [status, setStatus] = useState(null)
  const [pending, setPending] = useState([])
  const [listId, setListId] = useState('2198')
  const [picked, setPicked] = useState(null)   // selected company list, if any
  // How many contacts to take. '' = Maximum (the whole list). The cap counts NEW
  // contacts, so running it twice at 100 pulls two hundred different people rather
  // than the same hundred — see hubspot_pull.py --limit.
  const [limit, setLimit] = useState('')
  const [customLimit, setCustomLimit] = useState('')
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [showLog, setShowLog] = useState(false)

  // 0-1 = contact list, 0-2 = company list.
  function selectList(l) {
    if (l.object_type_id === '0-2') {
      setPicked(l)
    } else {
      setPicked(null)
      setListId(l.list_id)
    }
  }

  async function refresh() {
    try {
      const [s, b] = await Promise.all([api.status(), api.batches('pending')])
      setStatus(s)
      setPending(b.batches)
    } catch (e) { setError(e.message) }
  }

  useEffect(() => { refresh() }, [])

  // '' -> null (Maximum), 'custom' -> the typed number, else the preset.
  const effectiveLimit = limit === '' ? null
    : limit === 'custom' ? (Number(customLimit) > 0 ? Number(customLimit) : null)
      : Number(limit)

  async function runIngest() {
    setRunning(true); setError(null); setResult(null)
    try {
      const r = await api.ingest(listId.trim(), effectiveLimit)
      setResult(r)
      if (!r.ok) setError(`Ingest failed at ${r.stage || 'unknown'} stage`)
      await refresh()
    } catch (e) { setError(e.message) }
    finally { setRunning(false) }
  }

  const log = result && [result.pull, result.init]
    .filter(Boolean)
    .map((s) => (s.stdout || '') + (s.stderr ? '\n[stderr]\n' + s.stderr : ''))
    .join('\n\n')

  return (
    <div>
      <p className="page-sub" style={{ marginTop: 0 }}>
        Pull a HubSpot list into the pipeline and batch it for the SDR sub-agents. This is the
        pool campaigns draw their accounts from.
      </p>

      <ErrorBanner error={error} />

      <div className="panel" style={{ marginBottom: 22 }}>
        <div className="section-h" style={{ marginTop: 0 }}>Search HubSpot lists</div>
        <ListPicker onSelect={selectList} />
      </div>

      {picked && <SourcePanel list={picked} onChanged={refresh} />}

      <div className="panel" style={{ marginBottom: 22 }}>
        <div className="section-h" style={{ marginTop: 0 }}>Pull a contact list</div>
        <div className="toolbar" style={{ marginBottom: 0 }}>
          <label className="field">
            HubSpot list ID
            <input value={listId} onChange={(e) => setListId(e.target.value)} style={{ width: 160 }} />
          </label>
          <label className="field">
            How many contacts
            <select value={limit} onChange={(e) => setLimit(e.target.value)}
              disabled={running} style={{ width: 150 }}>
              <option value="">Maximum</option>
              {[25, 50, 100, 250, 500, 1000].map((n) => (
                <option key={n} value={n}>{num(n)}</option>
              ))}
              <option value="custom">Custom…</option>
            </select>
          </label>
          {limit === 'custom' && (
            <label className="field">
              Exactly
              <input type="number" min="1" value={customLimit} placeholder="e.g. 75"
                disabled={running} style={{ width: 110 }}
                onChange={(e) => setCustomLimit(e.target.value)} />
            </label>
          )}
          <button onClick={runIngest} disabled={running || !listId.trim()}>
            {running ? <Spinner label="Pulling + batching…" /> : 'Run pull + init'}
          </button>
          <span className="muted" style={{ alignSelf: 'center' }}>
            Runs <span className="mono">hubspot_pull.py</span> then <span className="mono">sdr_batches.py init</span>. Copy is generated separately via <span className="mono">/sdr-batches</span>.
          </span>
        </div>
        <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          {effectiveLimit
            ? <>Takes up to <b>{num(effectiveLimit)}</b> contacts the pipeline doesn't already
              hold. Run it again to take the next {num(effectiveLimit)}.</>
            : <>Takes every ICP contact on the list. Contacts already in the pipeline are
              skipped, so re-running only adds what's new.</>}
        </div>

        {result && result.ok && (
          <div className="banner info" style={{ marginTop: 16, marginBottom: 0 }}>
            Added <b>{num(result.new_contacts)}</b> new contacts and <b>{num(result.new_batches)}</b> new batches.
            {result.new_contacts === 0 && ' (List already ingested — init is idempotent.)'}
            {result.remaining_in_crm > 0 && (
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                {num(result.remaining_in_crm)} more contacts are still un-pulled — run it
                again to take the next batch.
              </div>
            )}
          </div>
        )}
        {log && (
          <>
            <button className="ghost sm" style={{ marginTop: 12 }} onClick={() => setShowLog((v) => !v)}>
              {showLog ? 'Hide' : 'Show'} run log
            </button>
            {showLog && <div className="log">{log}</div>}
          </>
        )}
      </div>

      {status && (
        <div className="grid stat-grid" style={{ marginBottom: 22 }}>
          <Stat label="Total contacts" value={num(status.total_contacts)} />
          <Stat label="Enrolled" value={num(status.contacts_by_status.enrolled || 0)} tone="good" />
          <Stat label="Pending" value={num(status.contacts_by_status.pending || 0)} />
          <Stat label="Batches done" value={num(status.batches_by_status.done || 0)}
            sub={`${status.batches_by_status.pending || 0} pending`} />
        </div>
      )}

      <h2 className="section-h">Pending batches</h2>
      {pending.length === 0 ? (
        <div className="empty">No pending batches — everything generated has been processed.<br />
          Ingest a list above, then run <span className="mono">/sdr-batches</span> in Claude Code to generate copy.</div>
      ) : (
        <div className="panel" style={{ padding: 0 }}>
          <table>
            <thead><tr><th>Batch</th><th>Size</th><th>Status</th></tr></thead>
            <tbody>
              {pending.map((b) => (
                <tr key={b.batch_id}>
                  <td className="mono">#{b.batch_id}</td>
                  <td>{b.size}</td>
                  <td><span className="badge status-pending">pending</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
