import { useState } from 'react'

// Filtering and sorting for the Use tab's INDEX views — Campaigns, Hot targets and
// the Call list.
//
// Those three are universal on purpose: they span every campaign, because a rep
// works one list rather than one list per campaign. That is the right default and
// the wrong only option. "Just the funding-signal campaign", "only accounts nobody
// has called yet", "worst-scoring first so I can see what the model is dragging in"
// are all questions the same table can answer once it can be narrowed and re-ordered.
//
// Everything here is CLIENT-SIDE, over rows already loaded. That is only honest
// while the caller holds the whole set, so a view that caps its fetch has to say
// what the sort is covering — see CallList's "N of M loaded" and its Load-all.
//
// Presentational only: classes live in styles.css alongside .toolbar, so a filter
// bar looks like the rest of the console rather than like a bolt-on.

// ---- sorting ------------------------------------------------------------------

// Null/blank sorts LAST in both directions. Ascending-by-score otherwise opens on a
// wall of never-scored rows, which is not what "show me the weakest" means — the
// same convention batch_db.campaign_members already applies in SQL.
function compare(a, b, sign) {
  const an = a == null || a === ''
  const bn = b == null || b === ''
  if (an && bn) return 0
  if (an) return 1
  if (bn) return -1
  if (typeof a === 'number' && typeof b === 'number') return (a - b) * sign
  return String(a).localeCompare(String(b), undefined,
    { numeric: true, sensitivity: 'base' }) * sign
}

// `sort` is null for NATURAL order — the order the server sent. That matters for the
// call list, whose default is an account-diverse interleave that no column sort can
// reproduce, so "no sort" has to stay reachable rather than being a hidden third
// state of some column.
export function useSort(initial = null) {
  const [sort, setSort] = useState(initial)
  // First click sorts by whatever reads as "most interesting first" for that column
  // (desc for numbers, asc for names); the second flips it; the third clears back to
  // the natural order.
  function toggle(key, preferred = 'desc') {
    setSort((s) => {
      if (!s || s.key !== key) return { key, dir: preferred }
      if (s.dir === preferred) return { key, dir: preferred === 'desc' ? 'asc' : 'desc' }
      return initial
    })
  }
  return { sort, setSort, toggle, reset: () => setSort(initial) }
}

// Decorate-sort-undecorate: the accessor runs once per row, and the original index
// breaks ties so equal rows keep the server's ordering instead of shuffling.
export function sortRows(rows, sort, accessors) {
  const get = sort?.key ? accessors[sort.key] : null
  if (!get) return rows
  const sign = sort.dir === 'asc' ? 1 : -1
  return rows
    .map((r, i) => [get(r), i, r])
    .sort((x, y) => compare(x[0], y[0], sign) || x[1] - y[1])
    .map((x) => x[2])
}

export function SortTh({ id, label, width, sort, onSort, dir = 'desc', title, sortable = true }) {
  if (!sortable || !onSort) {
    return <th style={width ? { width } : undefined} title={title}>{label}</th>
  }
  const on = sort?.key === id
  return (
    <th style={width ? { width } : undefined}
      className={'th-sort' + (on ? ' on' : '')}
      aria-sort={on ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}>
      {/* The caret sits INSIDE the label rather than beside it. As a sibling flex
          item it overlapped any single-word header too wide for its column
          ("BUYERS" at 8%); inline, it just follows the text wherever the text
          goes. */}
      <button type="button" onClick={() => onSort(id, dir)}
        title={title || `Sort by ${label}`}>
        <span className="th-label">
          {label}
          <span className="caret" aria-hidden="true">
            {on ? (sort.dir === 'asc' ? '▲' : '▼') : '↕'}
          </span>
        </span>
      </button>
    </th>
  )
}

// ---- filter controls -----------------------------------------------------------

export function Search({ value, onChange, placeholder = 'Search…', label, width = 210 }) {
  const input = (
    <span className="search-wrap" style={{ width }}>
      <input type="search" value={value} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)} style={{ width: '100%' }} />
      {value && (
        <button type="button" className="clear" title="Clear" aria-label="Clear search"
          onClick={() => onChange('')}>×</button>
      )}
    </span>
  )
  return label ? <label className="field">{label}{input}</label> : input
}

// `options` is [{value, label, count}] — usually straight from facet() below.
export function Pick({ label, value, onChange, options, any = 'any', width = 150 }) {
  return (
    <label className="field">{label}
      <select value={value} onChange={(e) => onChange(e.target.value)} style={{ width }}>
        <option value="">{any}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}{o.count != null ? ` (${o.count})` : ''}
          </option>
        ))}
      </select>
    </label>
  )
}

export function Toggle({ checked, onChange, label, title }) {
  return (
    <label className={'f-toggle' + (checked ? ' on' : '')} title={title}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  )
}

// The count line every filtered table needs. A list that is quietly filtered is how
// someone concludes the data is missing, so the shown/total split is always stated
// and the reset is right next to it.
export function FilterCount({ shown, total, noun = 'rows', active, onReset, note }) {
  return (
    <span className="filter-count">
      {shown === total
        ? <>{total.toLocaleString()} {noun}</>
        : <><b>{shown.toLocaleString()}</b> of {total.toLocaleString()} {noun}</>}
      {note && <span className="hint" style={{ display: 'block' }}>{note}</span>}
      {active && onReset && (
        <button type="button" className="linklike" style={{ marginLeft: 8 }}
          onClick={onReset}>reset</button>
      )}
    </span>
  )
}

// ---- helpers -------------------------------------------------------------------

// Distinct values of one field with their counts, commonest first.
//
// Computed over the UNFILTERED set on purpose: options that vanish as you narrow
// leave you unable to widen again without clearing everything, and a count of 0 is
// more useful than a missing entry.
export function facet(rows, get, label) {
  const m = new Map()
  for (const r of rows) {
    const v = get(r)
    if (v == null || v === '') continue
    const k = String(v)
    if (!m.has(k)) m.set(k, { value: k, label: label ? label(v, r) : String(v), count: 0 })
    m.get(k).count += 1
  }
  return [...m.values()].sort((a, b) => b.count - a.count || a.label.localeCompare(b.label))
}

// Free-text match across the fields a person would actually type into a search box.
// Every term must match somewhere, so "acme vp" narrows rather than widens.
export function matcher(query, fields) {
  const terms = (query || '').trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (!terms.length) return () => true
  return (row) => {
    const hay = fields(row).filter(Boolean).join(' ').toLowerCase()
    return terms.every((t) => hay.includes(t))
  }
}
