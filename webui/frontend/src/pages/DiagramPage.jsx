import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num, pct } from '../components/ui.jsx'
import { BRAND, PERSONA_COLORS } from '../theme.js'

// Pillar 2 — See: orchestration topology. HubSpot list -> persona routing ->
// persona sub-agents -> Email Bison campaigns (email) + HeyReach (LinkedIn).
const PERSONA_COLOR = PERSONA_COLORS
const LINKEDIN_BLUE = '#0a66c2'
const AGENT = {
  'sales-leadership': 'sdr-sales-leadership',
  'revops': 'sdr-revops',
  'partnerships': 'sdr-partnerships',
  'sdr-bdr': 'sdr-sdr-bdr-leadership',
}

// Small inline channel icons (drawn, so they render consistently).
function EmailIcon({ x, y, color = BRAND.muted }) {
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x="0" y="0" width="16" height="12" rx="2" fill="none" stroke={color} strokeWidth="1.4" />
      <path d="M1,1 L8,7 L15,1" fill="none" stroke={color} strokeWidth="1.4" />
    </g>
  )
}
function LinkedInIcon({ x, y, size = 16 }) {
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x="0" y="0" width={size} height={size} rx="3" fill={LINKEDIN_BLUE} />
      <text x={size / 2} y={size - 4} textAnchor="middle" fill="#fff" fontSize={size - 5} fontWeight="700"
        fontFamily="Georgia, serif">in</text>
    </g>
  )
}

export default function DiagramPage() {
  const [rollup, setRollup] = useState(null)
  const [error, setError] = useState(null)
  const [hover, setHover] = useState(null)

  useEffect(() => {
    api.rollup().then(setRollup).catch((e) => setError(e.message))
  }, [])

  const W = 1120
  const rowH = 118
  const top = 48
  const personas = rollup?.personas || []
  const linkedin = rollup?.linkedin || null
  const H = top + personas.length * rowH + 28

  const xHub = 24, wHub = 132
  const xPersona = 250, wPersona = 210
  const xCamp = 560, wCamp = 240
  const xLink = 900, wLink = 196
  const hubY = top + (personas.length * rowH) / 2 - 40
  const linkY = top + (personas.length * rowH) / 2 - 52
  const linkCY = linkY + 52

  return (
    <div>
      <h1 className="page-title">Orchestration</h1>
      <p className="page-sub">How contacts route from a HubSpot list through persona sub-agents into Email Bison campaigns (email) and HeyReach (LinkedIn).</p>
      <ErrorBanner error={error} />
      {!rollup ? <Spinner label="Loading…" /> : (
        <div className="panel" style={{ overflowX: 'auto' }}>
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ minWidth: 900 }}>
            <defs>
              <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill={BRAND.jade} />
              </marker>
              <marker id="arrowLi" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill={LINKEDIN_BLUE} />
              </marker>
            </defs>

            {/* HubSpot source node */}
            <g>
              <rect x={xHub} y={hubY} width={wHub} height={80} rx="12" fill="#fff" stroke={BRAND.border} />
              <text x={xHub + wHub / 2} y={hubY + 30} textAnchor="middle" fill={BRAND.ink} fontSize="14" fontWeight="700">HubSpot</text>
              <text x={xHub + wHub / 2} y={hubY + 50} textAnchor="middle" fill={BRAND.muted} fontSize="11">ICP list</text>
              <text x={xHub + wHub / 2} y={hubY + 67} textAnchor="middle" fill={BRAND.muted} fontSize="11">
                {num(personas.reduce((a, p) => a + p.contacts, 0))} contacts
              </text>
            </g>

            {/* LinkedIn / HeyReach channel — a single campaign every persona feeds */}
            {linkedin && (
              <g opacity={hover ? 0.5 : 1} style={{ transition: 'opacity .15s' }}>
                <rect x={xLink} y={linkY} width={wLink} height={104} rx="12" fill="#f4f8fd" stroke={LINKEDIN_BLUE} strokeWidth="1.5" />
                <LinkedInIcon x={xLink + 14} y={linkY + 14} />
                <text x={xLink + 38} y={linkY + 26} fill={BRAND.ink} fontSize="13" fontWeight="700">LinkedIn</text>
                <text x={xLink + 16} y={linkY + 52} fill={BRAND.ink} fontSize="13" fontWeight="700">
                  {linkedin.campaign_name || `HeyReach #${linkedin.campaign_id}`}
                </text>
                <text x={xLink + 16} y={linkY + 72} fill={BRAND.muted} fontSize="11">HeyReach · #{linkedin.campaign_id}</text>
                <text x={xLink + 16} y={linkY + 90} fill={LINKEDIN_BLUE} fontSize="11" fontWeight="700">{num(linkedin.leads)} leads</text>
              </g>
            )}

            {personas.map((p, i) => {
              const y = top + i * rowH
              const color = PERSONA_COLOR[p.persona] || BRAND.muted
              const dim = hover && hover !== p.persona
              const cy = y + 40
              const enrolled = p.by_status.enrolled || 0
              const stats = p.campaign_stats
              return (
                <g key={p.persona} opacity={dim ? 0.25 : 1}
                  onMouseEnter={() => setHover(p.persona)} onMouseLeave={() => setHover(null)}
                  style={{ transition: 'opacity .15s' }}>
                  {/* edges: hub -> persona, persona -> email campaign, persona -> LinkedIn */}
                  <path d={`M${xHub + wHub},${hubY + 40} C${xPersona - 40},${hubY + 40} ${xPersona - 40},${cy} ${xPersona},${cy}`}
                    fill="none" stroke={color} strokeWidth="2" markerEnd="url(#arrow)" opacity="0.7" />
                  <line x1={xPersona + wPersona} y1={cy} x2={xCamp} y2={cy}
                    stroke={color} strokeWidth="2" markerEnd="url(#arrow)" opacity="0.7" />
                  {linkedin && (
                    <path d={`M${xCamp + wCamp},${cy} C${xLink - 40},${cy} ${xLink - 40},${linkCY} ${xLink},${linkCY}`}
                      fill="none" stroke={LINKEDIN_BLUE} strokeWidth="1.5" markerEnd="url(#arrowLi)"
                      opacity={hover === p.persona ? 0.8 : 0.28} strokeDasharray="4 3" />
                  )}

                  {/* persona / agent node */}
                  <rect x={xPersona} y={y} width={wPersona} height={80} rx="12" fill="#fff" stroke={color} strokeWidth="1.5" />
                  <text x={xPersona + 16} y={y + 26} fill={BRAND.ink} fontSize="14" fontWeight="700">{p.persona}</text>
                  <text x={xPersona + 16} y={y + 45} fill={BRAND.muted} fontSize="11">{AGENT[p.persona]}</text>
                  <text x={xPersona + 16} y={y + 65} fill={color} fontSize="12" fontWeight="700">{num(p.contacts)} contacts</text>
                  <text x={xPersona + wPersona - 16} y={y + 65} textAnchor="end" fill={BRAND.muted} fontSize="11">{num(enrolled)} enrolled</text>

                  {/* email campaign node (name + channel icon) */}
                  <rect x={xCamp} y={y} width={wCamp} height={80} rx="12" fill="#fbfcfb" stroke={BRAND.border} />
                  <EmailIcon x={xCamp + 16} y={y + 16} color={BRAND.jade} />
                  <text x={xCamp + 40} y={y + 26} fill={BRAND.ink} fontSize="13" fontWeight="700">
                    {p.campaign_name || (p.campaign_id != null ? `Campaign #${p.campaign_id}` : '—')}
                  </text>
                  {p.campaign_name && p.campaign_id != null && (
                    <text x={xCamp + wCamp - 14} y={y + 26} textAnchor="end" fill={BRAND.muted} fontSize="10">#{p.campaign_id}</text>
                  )}
                  {stats ? (
                    <>
                      <text x={xCamp + 16} y={y + 50} fill={BRAND.muted} fontSize="11">{num(stats.total_leads_contacted)} contacted · {num(stats.interested)} interested</text>
                      <text x={xCamp + 16} y={y + 68} fill={BRAND.jade} fontSize="11">{pct(stats.reply_rate_pct)} reply · {pct(stats.interested_rate_pct)} interested</text>
                    </>
                  ) : (
                    <text x={xCamp + 16} y={y + 54} fill={BRAND.muted} fontSize="11">no cached stats — refresh Analytics</text>
                  )}
                </g>
              )
            })}
          </svg>
          <div className="row" style={{ gap: 18, marginTop: 10, fontSize: 12, alignItems: 'center', flexWrap: 'wrap' }}>
            <span className="muted">Channels:</span>
            <span className="row" style={{ gap: 6, alignItems: 'center' }}>
              <svg width="18" height="14"><EmailIcon x={1} y={1} color={BRAND.jade} /></svg> Email (Bison)
            </span>
            <span className="row" style={{ gap: 6, alignItems: 'center' }}>
              <svg width="18" height="16"><LinkedInIcon x={1} y={0} /></svg> LinkedIn (HeyReach)
            </span>
            <span className="muted">· Campaign stats appear when a Bison campaign id has a cached snapshot. Hover a row to isolate its path.</span>
          </div>
        </div>
      )}
    </div>
  )
}
