import { useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'

// Drop an event list (CSV or Excel) and make it a campaign audience.
//
// The gap this closes: a badge-scan export or a webinar list is the freshest
// audience there is and the only one the CRM has never heard of. Until it is
// imported it cannot be sequenced, scored or enriched — and the window where it is
// worth working is days, not quarters.
//
// Two steps on purpose. The first parses and counts and writes NOTHING; the second
// creates. Between them sits the column mapping, because an email column mapped to
// the wrong header imports a list of nobody, and contacts created in someone's CRM
// have no undo.
//
// The counts under the mapping are the decision. "482 rows" is not a decision;
// "310 have an email, 244 pass the ICP filter, 190 are people you don't already
// hold" is.

const FIELD_LABEL = {
  email: 'Email', first_name: 'First name', last_name: 'Last name',
  name: 'Full name', title: 'Job title', company: 'Company', domain: 'Domain / website',
  linkedin_url: 'LinkedIn URL', phone: 'Phone',
}
const ACCEPT = '.csv,.tsv,.txt,.xlsx,.xlsm'

function readBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onerror = () => reject(new Error(`Could not read ${file.name}`))
    r.onload = () => resolve(String(r.result).split(',')[1] || '')
    r.readAsDataURL(file)
  })
}

export default function ContactUpload({ onImported }) {
  const [file, setFile] = useState(null)          // {name, b64}
  const [preview, setPreview] = useState(null)
  const [mapping, setMapping] = useState({})
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [done, setDone] = useState(null)
  const [dropping, setDropping] = useState(false)
  const inputRef = useRef(null)

  async function take(list) {
    const f = (list || [])[0]
    if (!f) return
    setError(null); setDone(null); setBusy('parse')
    try {
      const b64 = await readBase64(f)
      const picked = { name: f.name, b64 }
      setFile(picked)
      setLabel(f.name.replace(/\.[^.]+$/, ''))
      const p = await api.uploadPreview({ filename: f.name, content_b64: b64 })
      setPreview(p)
      setMapping(p.mapping || {})
    } catch (e) { setError(e.message); setPreview(null) }
    finally { setBusy(null) }
  }

  // Re-run the preview whenever the mapping changes: the counts ARE the feedback on
  // whether the mapping is right, so they have to move with it.
  async function remap(field, header) {
    const next = { ...mapping }
    if (header) next[field] = header; else delete next[field]
    setMapping(next)
    if (!file) return
    setBusy('parse'); setError(null)
    try {
      setPreview(await api.uploadPreview({
        filename: file.name, content_b64: file.b64, mapping: next,
      }))
    } catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  async function commit() {
    setBusy('import'); setError(null)
    try {
      const r = await api.uploadImport({
        filename: file.name, content_b64: file.b64, mapping, label: label.trim(),
      })
      setDone(r)
      onImported?.(r)
    } catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  const s = preview?.stats || {}
  const headers = preview?.headers || []

  if (done) {
    return (
      <div className="banner info" style={{ marginTop: 12, marginBottom: 0 }}>
        Imported <b>{num(done.contacts)}</b> contacts from <b>{done.label}</b>
        {done.source?.created ? <> — <b>{num(done.source.created)}</b> created</> : null}.
        {' '}They're the audience for this campaign now.
        <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          Next: <b>Find more of the buyer group</b> on the Find accounts tab — an event
          list is usually one name at a company where several people matter.
        </div>
        <button className="ghost sm" style={{ marginTop: 10 }}
          onClick={() => { setDone(null); setFile(null); setPreview(null) }}>
          Import another file
        </button>
      </div>
    )
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <ErrorBanner error={error} />

      <div className={'dropzone' + (dropping ? ' on' : '')}
        onDragOver={(e) => { e.preventDefault(); setDropping(true) }}
        onDragLeave={() => setDropping(false)}
        onDrop={(e) => { e.preventDefault(); setDropping(false); take(e.dataTransfer.files) }}
        onClick={() => inputRef.current?.click()}>
        <input ref={inputRef} type="file" hidden accept={ACCEPT}
          onChange={(e) => { take(e.target.files); e.target.value = '' }} />
        {busy === 'parse' ? <Spinner label="Reading the file…" /> : (
          <>
            <div style={{ fontSize: 13, fontWeight: 600 }}>
              {file ? file.name : 'Drop a CSV or Excel file'}
            </div>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 3 }}>
              An event list, a webinar export, badge scans. Contacts are created in the
              CRM and the pipeline, deduped by email.
            </div>
          </>
        )}
      </div>

      {preview && (
        <>
          <div className="map-grid">
            {(preview.fields || []).map((f) => (
              <label key={f} className="field">
                {FIELD_LABEL[f] || f}{f === 'email' && <span className="req"> *</span>}
                <select value={mapping[f] || ''} disabled={!!busy}
                  onChange={(e) => remap(f, e.target.value)}>
                  <option value="">— not in this file —</option>
                  {headers.map((h) => <option key={h} value={h}>{h}</option>)}
                </select>
              </label>
            ))}
          </div>

          {/* What the import would actually do. The gap between rows and net_new is
              the whole reason this step exists. */}
          <div className="import-counts">
            <Count n={s.rows} label="rows in the file" />
            <Count n={s.with_email} label="with an email" />
            <Count n={s.icp} label="pass the ICP filter"
              sub={s.non_icp ? `${num(s.non_icp)} not a buyer we sell to` : null} />
            <Count n={s.net_new} label="new to the pipeline" tone="good"
              sub={s.already_in_pipeline
                ? `${num(s.already_in_pipeline)} you already hold`
                : null} />
            <Count n={s.accounts} label="accounts" />
          </div>

          {(preview.sample || []).length > 0 && (
            <div className="panel" style={{ padding: 0, marginTop: 10, overflowX: 'auto' }}>
              <table className="dense" style={{ width: '100%' }}>
                <thead><tr>
                  <th>Name</th><th>Title</th><th>Company</th><th>Email</th><th />
                </tr></thead>
                <tbody>
                  {preview.sample.map((c) => (
                    <tr key={c.email}>
                      <td>{`${c.first_name} ${c.last_name}`.trim() || '—'}</td>
                      <td className="muted">{c.title || '—'}</td>
                      <td className="muted">{c.company || c.domain || '—'}</td>
                      <td className="muted mono" style={{ fontSize: 11.5 }}>{c.email}</td>
                      <td>{c.in_pipeline
                        ? <span className="badge">already held</span>
                        : <span className="badge status-enrolled">new</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="card-actions">
            <label className="field">
              Call this list
              <input value={label} onChange={(e) => setLabel(e.target.value)}
                placeholder="SaaStr 2026 — booth scans" style={{ width: 260 }} />
            </label>
            <button className="primary sm" disabled={!!busy || !s.icp || !mapping.email}
              onClick={commit}>
              {busy === 'import'
                ? <Spinner label="Creating contacts…" />
                : `Import ${num(s.icp || 0)} contacts`}
            </button>
            <span className="hint">
              Creates the {num(s.net_new || 0)} new ones in the CRM and the pipeline;
              the rest are matched by email, not duplicated.
            </span>
          </div>
        </>
      )}
    </div>
  )
}

function Count({ n, label, sub, tone }) {
  return (
    <div className="import-count">
      <div className="v" data-tone={tone}>{num(n || 0)}</div>
      <div className="l">{label}</div>
      {sub && <div className="s">{sub}</div>}
    </div>
  )
}
