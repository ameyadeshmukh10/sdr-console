import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner } from './ui.jsx'

// Searchable HubSpot list picker for the Use view. Toggles between contact lists
// (objectTypeId 0-1) and company lists (0-2); selecting one calls back with
// { list_id, name, object_type_id }.
const TYPES = [
  { id: 'contact', label: 'Contact lists' },
  { id: 'company', label: 'Company lists' },
]

export default function ListPicker({ onSelect }) {
  const [type, setType] = useState('contact')
  const [q, setQ] = useState('')
  const [results, setResults] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
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

  return (
    <div>
      <div className="row" style={{ gap: 8, marginBottom: 10 }}>
        {TYPES.map((t) => (
          <button key={t.id} className={type === t.id ? '' : 'ghost'}
            onClick={() => setType(t.id)} style={{ fontSize: 13 }}>{t.label}</button>
        ))}
      </div>
      <label className="field grow">Search HubSpot {type} lists
        <input placeholder="list name…" value={q} onChange={(e) => setQ(e.target.value)} />
      </label>
      {busy && <div style={{ marginTop: 8 }}><Spinner label="Searching…" /></div>}
      {error && <div className="banner error" style={{ marginTop: 8 }}>{error}</div>}
      {results && !busy && (
        <div className="panel" style={{ padding: 0, marginTop: 10, maxHeight: 260, overflow: 'auto' }}>
          {results.length === 0 ? (
            <div className="empty" style={{ padding: 16 }}>No {type} lists match “{q}”.</div>
          ) : (
            <table>
              <thead><tr><th>List</th><th>Size</th><th>ID</th><th></th></tr></thead>
              <tbody>
                {results.map((l) => (
                  <tr key={l.list_id}>
                    <td>{l.name}</td>
                    <td className="muted">{l.size ?? '—'}</td>
                    <td className="mono muted">{l.list_id}</td>
                    <td><button className="ghost sm" onClick={() => onSelect(l)}>Select</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
