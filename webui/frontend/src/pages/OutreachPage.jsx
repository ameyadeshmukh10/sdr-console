import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Badge, Spinner, ErrorBanner, num } from '../components/ui.jsx'
import OutreachDetail from '../components/OutreachDetail.jsx'

// Pillar 4 — Transparency: browse/search the generated outreach, filter and
// group by persona / CTA play / status / company, open any lead's full copy.
const PAGE_SIZE = 50

export default function OutreachPage() {
  const [filters, setFilters] = useState({ persona: '', cta: '', status: '', q: '', group_by: '' })
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [openId, setOpenId] = useState(null)

  useEffect(() => {
    setLoading(true)
    const params = { ...filters, page, page_size: PAGE_SIZE }
    Object.keys(params).forEach((k) => params[k] === '' && delete params[k])
    api.outreach(params)
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [filters, page])

  function setF(key, val) { setPage(1); setFilters((f) => ({ ...f, [key]: val })) }

  const facets = data?.facets || {}
  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  const facetOptions = (name) =>
    Object.entries(facets[name] || {}).map(([k, n]) => (
      <option key={k} value={k}>{k} ({n})</option>
    ))

  return (
    <div>
      <h1 className="page-title">Outreach</h1>
      <p className="page-sub">Every generated sequence, joined with contact + enrollment status. Click a row for the full copy.</p>

      <div className="toolbar">
        <label className="field grow">Search
          <input placeholder="name, email, company, signal…" value={filters.q}
            onChange={(e) => setF('q', e.target.value)} />
        </label>
        <label className="field">Persona
          <select value={filters.persona} onChange={(e) => setF('persona', e.target.value)}>
            <option value="">All</option>{facetOptions('persona')}
          </select>
        </label>
        <label className="field">CTA play
          <select value={filters.cta} onChange={(e) => setF('cta', e.target.value)}>
            <option value="">All</option>{facetOptions('cta_type')}
          </select>
        </label>
        <label className="field">Status
          <select value={filters.status} onChange={(e) => setF('status', e.target.value)}>
            <option value="">All</option>{facetOptions('status')}
          </select>
        </label>
        <label className="field">Group by
          <select value={filters.group_by} onChange={(e) => setF('group_by', e.target.value)}>
            <option value="">— none —</option>
            <option value="persona">Persona</option>
            <option value="cta_type">CTA play</option>
            <option value="status">Status</option>
            <option value="company">Company</option>
          </select>
        </label>
      </div>

      <ErrorBanner error={error} />

      {data?.groups && (
        <div className="panel" style={{ marginBottom: 18 }}>
          <div className="section-h" style={{ margin: '0 0 12px' }}>Grouped by {filters.group_by} — {Object.keys(data.groups).length} groups</div>
          <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
            {Object.entries(data.groups).slice(0, 40).map(([k, n]) => (
              <span key={k} className="badge">{k || '—'}: {n}</span>
            ))}
          </div>
        </div>
      )}

      <div className="row between" style={{ marginBottom: 10 }}>
        <span className="muted">{loading ? <Spinner /> : `${num(data?.total || 0)} sequences`}</span>
      </div>

      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr><th>Name</th><th>Company</th><th>Persona</th><th>CTA play</th><th>Status</th><th>Signal</th></tr>
          </thead>
          <tbody>
            {data?.items?.map((r) => (
              <tr key={r.contact_id} className="clickable" onClick={() => setOpenId(r.contact_id)}>
                <td>{r.first_name} {r.last_name}</td>
                <td>{r.company || <span className="muted">—</span>}</td>
                <td><Badge kind="persona" value={r.persona} /></td>
                <td><span className="badge cta">{r.cta_type}</span></td>
                <td><Badge kind="status" value={r.status} /></td>
                <td className="muted" style={{ maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.signal}</td>
              </tr>
            ))}
            {data && data.items.length === 0 && !loading && (
              <tr><td colSpan={6}><div className="empty">No matching sequences.</div></td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="pager">
        <button className="ghost sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>← Prev</button>
        <span className="muted">Page {page} / {totalPages}</span>
        <button className="ghost sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next →</button>
      </div>

      {openId && <OutreachDetail id={openId} onClose={() => setOpenId(null)} />}
    </div>
  )
}
