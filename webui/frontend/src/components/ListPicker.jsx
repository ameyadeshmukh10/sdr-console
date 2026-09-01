import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner } from './ui.jsx'

// Searchable HubSpot list picker for the Use view: one dropdown (contact / company
// lists) inline with the search box; the result table is collapsed by default and
// opens when you search or click "show lists". Selecting one calls back with
// { list_id, name, object_type_id, size, created_at, pulled_at?… }. Results come
// newest-created first; lists already pulled carry a badge + the Pulled column.
const TYPES = [
  { id: 'contact', label: 'Contact lists' },
  { id: 'company', label: 'Company lists' },
]

function fmtDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export default function ListPicker({ onSelect, selectedId }) {
  const [type, setType] = useState('contact')
  const [q, setQ] = useState('')
  const [results, setResults] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(false)   // collapsed by default — open on search or click
  const debounce = useRef(null)

  useEffect(() => {
    clearTimeout(debounce.current)
    debounce.current = setTimeout(async () => {
      setBusy(true); setError(null)
      try {
        const r = await api.hubspotLists(q, type)
        if (r.ok === false) setError(r.error || 'search failed')
        setResults(r.lists || [])
      } catch (e) { setError(e.message) }
      finally { setBusy(false) }
    }, 300)
    return () => clearTimeout(debounce.current)
  }, [q, type])

  const count = results ? results.length : null
  return (
    <div>
      <div className="row" style={{ gap: 8, alignItems: 'center' }}>
        <select value={type} onChange={(e) => setType(e.target.value)} style={{ width: 150, fontSize: 13 }}>
          {TYPES.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
        </select>
        <input placeholder="Search" value={q} onChange={(e) => { setQ(e.target.value); setOpen(true) }} style={{ flex: 1 }} />
        <button className="ghost sm" onClick={() => setOpen((v) => !v)} disabled={busy && !results}>
          {open ? 'hide lists' : `show lists${count != null ? ` (${count})` : ''}`}
        </button>
      </div>
      {busy && open && <div style={{ marginTop: 8 }}><Spinner label="Searching…" /></div>}
      {error && <div className="banner error" style={{ marginTop: 8 }}>{error}</div>}
      {open && results && !busy && (
        <div className="panel" style={{ padding: 0, marginTop: 10, maxHeight: 240, overflow: 'auto' }}>
          {results.length === 0 ? (
            <div className="empty" style={{ padding: 16 }}>No {type} lists match{q ? ` “${q}”` : ''}.</div>
          ) : (
            <table className="dense">
              <thead><tr><th>List</th><th>Created</th><th>Size</th><th>Pulled</th><th>ID</th><th></th></tr></thead>
              <tbody>
                {results.map((l) => {
                  const active = String(selectedId) === String(l.list_id)
                  const pulled = !!l.pulled_at
                  return (
                    <tr key={l.list_id} style={active ? { background: 'var(--accent-dim)' } : (pulled ? { background: 'var(--surface-hover)' } : undefined)}>
                      <td>
                        {l.name}
                        {pulled && <span className="badge status-enrolled" style={{ marginLeft: 8, fontSize: 10 }}
                          title={`Already pulled into the pipeline${l.pulled_source && l.pulled_source !== 'manual' ? ` by ${l.pulled_source}` : ''}${l.pulls > 1 ? ` (${l.pulls}×)` : ''}`}>pulled</span>}
                      </td>
                      <td className="muted" style={{ whiteSpace: 'nowrap' }} title={l.created_at || ''}>{fmtDate(l.created_at)}</td>
                      <td className="muted">{l.size ?? '—'}</td>
                      <td className="muted" style={{ whiteSpace: 'nowrap' }} title={pulled ? `${l.pulled_at} · ${l.pulled_kept ?? '?'} kept of ${l.pulled_read ?? '?'} read` : 'never pulled'}>
                        {pulled ? <>{fmtDate(l.pulled_at)}{l.pulled_kept != null ? <span style={{ fontSize: 11 }}> · {l.pulled_kept} kept</span> : null}</> : '—'}
                      </td>
                      <td className="mono muted">{l.list_id}</td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        <button className={active ? 'sm' : 'ghost sm'} onClick={() => { onSelect(l); setOpen(false) }}>
                          {active ? '✓ Selected' : (pulled ? 'Pull again' : 'Select')}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
