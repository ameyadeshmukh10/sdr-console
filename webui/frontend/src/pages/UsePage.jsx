import { useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from '../components/ui.jsx'
import ListPicker from '../components/ListPicker.jsx'
import SourcePanel from '../components/SourcePanel.jsx'
import SlaPanel from '../components/SlaPanel.jsx'

// Pillar 1 — Use: feed a HubSpot list into the pipeline. Contact lists pull +
// init directly from the picker (Select → confirm → run, all in one place);
// company lists enrich the buying group via Clay first. Batch progress and the
// pipeline stats live on the Pipeline tab — this page is only "get contacts in".
export default function UsePage() {
  const [picked, setPicked] = useState(null)      // {list_id, name, object_type_id, size}
  const [manualId, setManualId] = useState('')     // fallback: type an id by hand
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [showLog, setShowLog] = useState(false)

  const isCompany = picked?.object_type_id === '0-2'
  const contactListId = (isCompany ? '' : (picked?.list_id || manualId)).toString().trim()

  async function runIngest() {
    if (!contactListId) return
    setRunning(true); setError(null); setResult(null)
    try {
      const r = await api.ingest(contactListId)
      setResult(r)
      if (!r.ok) setError(`Ingest failed at ${r.stage || 'unknown'} stage`)
    } catch (e) { setError(e.message) }
    finally { setRunning(false) }
  }

  const log = result && [result.pull, result.init]
    .filter(Boolean)
    .map((s) => (s.stdout || '') + (s.stderr ? '\n[stderr]\n' + s.stderr : ''))
    .join('\n\n')

  return (
    <div>
      <h1 className="page-title" style={{ marginBottom: 18 }}>Select Target Audience</h1>

      <ErrorBanner error={error} />

      <div className="panel" style={{ marginBottom: 22 }}>
        <div className="section-h" style={{ marginTop: 0 }}>CRM List</div>
        <ListPicker onSelect={(l) => { setPicked(l); setResult(null); setError(null) }} selectedId={picked?.list_id} />

        {/* Step 2 lives right here — no second panel to hunt for. */}
        {picked && !isCompany && (
          <div className="banner info" style={{ marginTop: 14, marginBottom: 0 }}>
            <div className="row between" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
              <span>
                <b>2 · Selected:</b> {picked.name} <span className="mono muted">#{picked.list_id}</span>
                {picked.size != null && <span className="muted"> · {num(picked.size)} contacts</span>}
                <span className="muted"> — pulls the list (<span className="mono">hubspot_pull.py</span>) then batches it (<span className="mono">sdr_batches.py init</span>).</span>
              </span>
              <span className="row" style={{ gap: 8 }}>
                <button onClick={runIngest} disabled={running}>
                  {running ? <Spinner label="Pulling + batching…" /> : 'Confirm — run pull + init'}
                </button>
                <button className="ghost sm" onClick={() => setPicked(null)} disabled={running}>Cancel</button>
              </span>
            </div>
          </div>
        )}

        {!picked && (
          <details style={{ marginTop: 12 }}>
            <summary className="muted" style={{ fontSize: 12.5, cursor: 'pointer' }}>Or enter a contact list ID by hand</summary>
            <div className="row" style={{ gap: 8, marginTop: 8, alignItems: 'center' }}>
              <input value={manualId} onChange={(e) => setManualId(e.target.value)} placeholder="e.g. 2198" style={{ width: 140 }} />
              <button className="ghost sm" onClick={runIngest} disabled={running || !manualId.trim()}>
                {running ? <Spinner label="Pulling + batching…" /> : 'Run pull + init'}
              </button>
            </div>
          </details>
        )}

        {result && result.ok && (
          <div className="banner info" style={{ marginTop: 14, marginBottom: 0 }}>
            Added <b>{num(result.new_contacts)}</b> new contacts and <b>{num(result.new_batches)}</b> new batches.
            {result.new_contacts === 0 && ' (List already ingested — init is idempotent.)'}
            {' '}Generate copy on the <b>Pipeline</b> tab.
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

      {picked && isCompany && <SourcePanel list={picked} />}

      <SlaPanel />
    </div>
  )
}
