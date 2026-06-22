// EverWorker brand colors for charts/diagrams (SVG + recharts can't read CSS vars).
export const BRAND = {
  ink: '#0f1c18',
  muted: 'rgba(15,28,24,0.55)',
  mint: '#52febf',
  jade: '#22826f',
  jadeDeep: '#1c826e',
  teal: '#33b690',
  grid: 'rgba(15,28,24,0.08)',
  border: 'rgba(15,28,24,0.12)',
  panel: '#ffffff',
  bg: '#f6f6f4',
  amber: '#b8791f',
  violet: '#6d5dd3',
  red: '#dc2626',
}

// persona hues — mirror styles.css --p-* tokens
export const PERSONA_COLORS = {
  'sales-leadership': '#146c7a',
  'revops': '#2f6db5',
  'partnerships': '#6d5dd3',
  'sdr-bdr': '#b8791f',
}

// ordered series palette for multi-series charts (green-led, brand-harmonized)
export const SERIES = ['#22826f', '#52febf', '#33b690', '#6d5dd3', '#b8791f', '#2f6db5', '#1c826e']

// recharts tooltip style on light surfaces
export const TOOLTIP_STYLE = {
  background: '#ffffff',
  border: '1px solid rgba(15,28,24,0.12)',
  borderRadius: 10,
  color: '#0f1c18',
  boxShadow: '0 4px 16px rgba(15,28,24,0.08)',
}
