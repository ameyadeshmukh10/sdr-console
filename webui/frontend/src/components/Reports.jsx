import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import Addon from './Addon.jsx'

// Raw data, with configurable columns — and a report you can just describe.
//
// The curated panels answer the questions we anticipated. This answers the ones we
// didn't, which is most of them. Shared by Analytics and Trends because it is the
// same feature on both: pick a dataset, choose columns, filter, group — or type the
// question and let the model assemble the spec.
//
// The model never writes SQL. It emits a constrained SPEC (dataset id, column ids,
// operators from an allowlist) that is validated server-side before anything runs,
// so a bad or adversarial description fails validation rather than reaching data it
// shouldn't. See webui/server/reports.py.

const EXAMPLES = [
  'Hot contacts we haven’t worked yet',
  'Signals by type over the last month',
  'Campaign performance, best first',
  'Credit spend by provider',
]

export default function Reports() {
  const [schema, setSchema] = useState(null)
  const [dataset, setDataset] = useState('contacts')
  const [cols, setCols] = useState([])
  const [result, setResult] = useState(null)
  const [desc, setDesc] = useState('')
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [showCols, setShowCols] = useState(false)

  useEffect(() => { api.reportSchema().then(setSchema).catch((e) => setError(e.message)) }, [])

  const ds = (schema?.datasets || []).find((d) => d.id === dataset)

  // Reset the column choice whenever the dataset changes — a column id from the
  // previous dataset would fail validation, which is correct but unhelpful here.
  useEffect(() => { if (ds) setCols(ds.default_columns) }, [dataset, !!ds])

  async function run(spec) {
    setBusy('run'); setError(null)
    try {
      const r = await api.runReport(spec)
      if (r.ok === false) setError(r.error)
      setResult(r.ok === false ? null : r)
    } catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  async function describe() {
    if (!desc.trim()) return
    setBusy('desc'); setError(null)
    try {
      const r = await api.describeReport(desc)
      if (r.ok === false) { setError(r.error); setResult(null) }
      else {
        setResult(r)
        if (r.spec?.dataset) setDataset(r.spec.dataset)
        if (r.columns) setCols(r.columns)
      }
    } catch (e) {
      setError(e.status === 501
        ? 'Generating a report from a description needs ANTHROPIC_API_KEY on the server.'
        : e.message)
    } finally { setBusy(null) }
  }

  function toggleCol(id) {
    setCols(cols.includes(id) ? cols.filter((c) => c !== id) : [...cols, id])
  }

  const rows = result?.rows || []
  const outCols = result?.columns || []

  return (
    <div>
      <div className="row" style={{ gap: 8, marginBottom: 2 }}>
        <Addon id="advanced-analytics" />
      </div>
      <p className="page-sub" style={{ marginTop: 0 }}>
        The underlying rows, with the columns you want — or describe the report and it
        gets built for you.
      </p>

      <ErrorBanner error={error} />

      <div className="panel" style={{ marginBottom: 18 }}>
        <div className="card-h"><h3>Describe a report</h3></div>
        <p className="card-note">
          Plain English. The request is turned into a validated query over the datasets
          below — if it can’t be answered from them, it says so rather than guessing.
        </p>
        <div className="card-actions" style={{ marginTop: 12 }}>
          <input value={desc} onChange={(e) => setDesc(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && describe()}
            placeholder="e.g. hot contacts at accounts we haven't contacted yet"
            style={{ flex: 1, minWidth: 260 }} />
          <button className="primary sm" disabled={busy || !desc.trim()} onClick={describe}>
            {busy === 'desc' ? <Spinner /> : 'Build it'}
          </button>
        </div>
        <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginTop: 10 }}>
          {EXAMPLES.map((x) => (
            <button key={x} type="button" className="tag"
              style={{ cursor: 'pointer', border: '1px solid var(--border-strong)' }}
              onClick={() => { setDesc(x); }}>{x}</button>
          ))}
        </div>
        {result?.matched && (
          <p className="hint" style={{ marginTop: 10 }}>
            Showing <b>{result.matched}</b>, computed from this profile’s data.
          </p>
        )}
      </div>

      {!schema ? <Spinner label="Loading…" /> : (
        <>
          <div className="toolbar">
            <label className="field">Dataset
              <select value={dataset} onChange={(e) => { setDataset(e.target.value); setResult(null) }}>
                {schema.datasets.map((d) => (
                  <option key={d.id} value={d.id}>{d.label}</option>
                ))}
              </select>
            </label>
            <div className="grow" />
            <button className="ghost sm" onClick={() => setShowCols(!showCols)}>
              Columns ({cols.length})
            </button>
            <button className="primary sm" disabled={busy}
              onClick={() => run({ dataset, columns: cols, limit: 200 })}>
              {busy === 'run' ? <Spinner /> : 'Show rows'}
            </button>
          </div>

          {ds && <p className="hint" style={{ marginTop: -8, marginBottom: 12 }}>{ds.describe}</p>}

          {showCols && ds && (
            <div className="panel" style={{ marginBottom: 16 }}>
              <div className="section-h" style={{ marginTop: 0 }}>Columns</div>
              <div className="col-list" style={{ gridTemplateColumns: 'repeat(auto-fit,minmax(190px,1fr))', display: 'grid' }}>
                {ds.columns.map((c) => (
                  <label key={c.id} className="f-check">
                    <input type="checkbox" checked={cols.includes(c.id)}
                      onChange={() => toggleCol(c.id)} />
                    <span>{c.label}<span className="hint">{c.type}</span></span>
                  </label>
                ))}
              </div>
            </div>
          )}

          {result && (
            <>
              <div className="card-h" style={{ marginBottom: 8 }}>
                <div>
                  <h3>{result.spec?.title || ds?.label}</h3>
                  <p className="card-note">
                    {num(result.count)} rows
                    {result.truncated && ' (truncated — narrow the filters for more)'}
                    {result.spec?.group_by && ` · grouped by ${result.spec.group_by}`}
                  </p>
                </div>
              </div>
              {rows.length === 0 ? (
                <div className="empty">No rows match.</div>
              ) : (
                <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
                  <table className="dense" style={{ width: '100%',
                    minWidth: Math.max(600, outCols.length * 140) }}>
                    <thead><tr>
                      {outCols.map((c) => <th key={c}>{result.labels?.[c] || c}</th>)}
                    </tr></thead>
                    <tbody>
                      {rows.map((r, i) => (
                        <tr key={i}>
                          {outCols.map((c) => (
                            <td key={c} className={typeof r[c] === 'number' ? undefined : 'muted'}>
                              {r[c] == null ? '—' : String(r[c])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}
