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

// `accent` adds a brand-gradient left rule; `tone` ("good" | "warn" | "bad")
// colors the value. Both are optional and default to the existing plain look.
export function Stat({ label, value, sub, accent = false, tone }) {
  return (
    <div className={'stat' + (accent ? ' accent' : '')} {...(tone ? { 'data-tone': tone } : {})}>
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

// Channel icons shared by the Analytics section headers and the Orchestration
// diagram. Both render inside an <svg> (they are <g> groups, not elements).
export const LINKEDIN_BLUE = '#0a66c2'

export function EmailIcon({ x, y, color = 'var(--muted)' }) {
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x="0" y="0" width="16" height="12" rx="2" fill="none" stroke={color} strokeWidth="1.4" />
      <path d="M1,1 L8,7 L15,1" fill="none" stroke={color} strokeWidth="1.4" />
    </g>
  )
}

export function LinkedInIcon({ x, y, size = 16 }) {
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x="0" y="0" width={size} height={size} rx="3" fill={LINKEDIN_BLUE} />
      <text x={size / 2} y={size - 4} textAnchor="middle" fill="#fff" fontSize={size - 5} fontWeight="700"
        fontFamily="Georgia, serif">in</text>
    </g>
  )
}

export function pct(v) {
  return v == null ? '—' : `${v}%`
}

export function num(v) {
  return v == null ? '—' : v.toLocaleString()
}
