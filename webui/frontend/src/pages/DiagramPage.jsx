import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num, pct } from '../components/ui.jsx'

// Pillar 2 — See: orchestration topology. HubSpot list -> persona routing ->
// persona sub-agents -> Bison campaigns, with live contact counts overlaid.
const PERSONA_COLOR = {
  'sales-leadership': '#4f9dff',
  'revops': '#3fb950',
  'partnerships': '#bc8cff',
  'sdr-bdr': '#d29922',
}
const AGENT = {
  'sales-leadership': 'sdr-sales-leadership',
  'revops': 'sdr-revops',
  'partnerships': 'sdr-partnerships',
  'sdr-bdr': 'sdr-sdr-bdr-leadership',
}

export default function DiagramPage() {
  const [rollup, setRollup] = useState(null)
  const [error, setError] = useState(null)
  const [hover, setHover] = useState(null)

  useEffect(() => {
    api.rollup().then(setRollup).catch((e) => setError(e.message))
  }, [])

  const W = 1000
  const rowH = 118
  const top = 40
  const personas = rollup?.personas || []
  const H = top + personas.length * rowH + 20

  const xHub = 30, wHub = 150
  const xPersona = 320, wPersona = 230
  const xCamp = 720, wCamp = 230
  const hubY = top + (personas.length * rowH) / 2 - 40

  return (
    <div>
      <h1 className="page-title">Orchestration</h1>
      <p className="page-sub">How contacts route from a HubSpot list through persona sub-agents into Email Bison campaigns.</p>
      <ErrorBanner error={error} />
      {!rollup ? <Spinner label="Loading…" /> : (
        <div className="panel" style={{ overflowX: 'auto' }}>
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ minWidth: 760 }}>
            <defs>
              <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill="#5a6675" />
              </marker>
            </defs>

            {/* HubSpot source node */}
            <g>
              <rect x={xHub} y={hubY} width={wHub} height={80} rx="10" fill="#161b22" stroke="#2a3340" />
              <text x={xHub + wHub / 2} y={hubY + 30} textAnchor="middle" fill="#e6edf3" fontSize="14" fontWeight="700">HubSpot</text>
              <text x={xHub + wHub / 2} y={hubY + 50} textAnchor="middle" fill="#8b97a6" fontSize="11">ICP list</text>
              <text x={xHub + wHub / 2} y={hubY + 67} textAnchor="middle" fill="#8b97a6" fontSize="11">
                {num(personas.reduce((a, p) => a + p.contacts, 0))} contacts
              </text>
            </g>

            {personas.map((p, i) => {
              const y = top + i * rowH
              const color = PERSONA_COLOR[p.persona] || '#8b97a6'
              const dim = hover && hover !== p.persona
              const cy = y + 40
              const enrolled = p.by_status.enrolled || 0
              const stats = p.campaign_stats
              return (
                <g key={p.persona} opacity={dim ? 0.3 : 1}
                  onMouseEnter={() => setHover(p.persona)} onMouseLeave={() => setHover(null)}
                  style={{ transition: 'opacity .15s' }}>
                  {/* edges */}
                  <path d={`M${xHub + wHub},${hubY + 40} C${xPersona - 40},${hubY + 40} ${xPersona - 40},${cy} ${xPersona},${cy}`}
                    fill="none" stroke={color} strokeWidth="2" markerEnd="url(#arrow)" opacity="0.7" />
                  <line x1={xPersona + wPersona} y1={cy} x2={xCamp} y2={cy}
                    stroke={color} strokeWidth="2" markerEnd="url(#arrow)" opacity="0.7" />

                  {/* persona / agent node */}
                  <rect x={xPersona} y={y} width={wPersona} height={80} rx="10" fill="#1c2330" stroke={color} strokeWidth="1.5" />
                  <text x={xPersona + 16} y={y + 26} fill="#e6edf3" fontSize="14" fontWeight="700">{p.persona}</text>
                  <text x={xPersona + 16} y={y + 45} fill="#8b97a6" fontSize="11">{AGENT[p.persona]}</text>
                  <text x={xPersona + 16} y={y + 65} fill={color} fontSize="12" fontWeight="600">{num(p.contacts)} contacts</text>
                  <text x={xPersona + wPersona - 16} y={y + 65} textAnchor="end" fill="#8b97a6" fontSize="11">{num(enrolled)} enrolled</text>

                  {/* campaign node */}
                  <rect x={xCamp} y={y} width={wCamp} height={80} rx="10" fill="#161b22" stroke="#2a3340" />
                  <text x={xCamp + 16} y={y + 26} fill="#e6edf3" fontSize="14" fontWeight="700">Campaign {p.campaign_id ?? '—'}</text>
                  {stats ? (
                    <>
                      <text x={xCamp + 16} y={y + 47} fill="#8b97a6" fontSize="11">{num(stats.total_leads_contacted)} contacted · {num(stats.interested)} interested</text>
                      <text x={xCamp + 16} y={y + 65} fill="#3fb950" fontSize="11">{pct(stats.reply_rate_pct)} reply · {pct(stats.interested_rate_pct)} interested</text>
                    </>
                  ) : (
                    <text x={xCamp + 16} y={y + 50} fill="#8b97a6" fontSize="11">no cached stats — refresh Analytics</text>
                  )}
                </g>
              )
            })}
          </svg>
          <p className="muted" style={{ marginTop: 10, fontSize: 12 }}>
            Campaign stats appear when a persona's Bison campaign id has a cached snapshot. Hover a row to isolate its path.
          </p>
        </div>
      )}
    </div>
  )
}
