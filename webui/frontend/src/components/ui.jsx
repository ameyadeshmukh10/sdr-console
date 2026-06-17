// Small shared presentational helpers used across pages.

export function Badge({ kind, value }) {
  if (!value) return <span className="muted">—</span>
  return <span className={`badge ${kind}-${value}`}>{value}</span>
}

export function Spinner({ label }) {
  return (
    <span className="row" style={{ gap: 8 }}>
      <span className="spinner" />{label && <span className="muted">{label}</span>}
    </span>
  )
}

export function Stat({ label, value, sub }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub != null && <div className="sub">{sub}</div>}
    </div>
  )
}

export function ErrorBanner({ error }) {
  if (!error) return null
  return <div className="banner error">{String(error)}</div>
}

export function pct(v) {
  return v == null ? '—' : `${v}%`
}

export function num(v) {
  return v == null ? '—' : v.toLocaleString()
}
