import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num, pct } from '../components/ui.jsx'
import { BRAND, PERSONA_COLORS } from '../theme.js'

// Pillar 2 — See: orchestration topology. HubSpot list -> persona routing ->
// persona sub-agents -> EVERY Bison campaign contacts actually route into (per
// contact: variant campaign first, persona campaign fallback) + HeyReach (LinkedIn).
const PERSONA_COLOR = PERSONA_COLORS
const LINKEDIN_BLUE = '#0a66c2'
const AGENT = {
  'sales-leadership': 'sdr-sales-leadership',
  'revops': 'sdr-revops',
  'partnerships': 'sdr-partnerships',
  'sdr-bdr': 'sdr-sdr-bdr-leadership',
}
// Accent per campaign node by how it's routed.
const KIND_COLOR = { variant: BRAND.jade, persona: BRAND.teal, unrouted: BRAND.red, campaign: BRAND.muted }

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

// horizontal-tangent bezier between two points (left edge -> right edge of nodes)
function curve(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`
}

export default function DiagramPage() {
  const [rollup, setRollup] = useState(null)
  const [error, setError] = useState(null)
  const [hover, setHover] = useState(null)   // {type:'persona'|'campaign', id}

  useEffect(() => {
    api.rollup().then(setRollup).catch((e) => setError(e.message))
  }, [])

  const personas = rollup?.personas || []
  const campaigns = rollup?.campaigns || []
  const edges = rollup?.edges || []
  const linkedin = rollup?.linkedin || null

  // ---- layout -------------------------------------------------------------
  const W = 1200
  const contentTop = 56
  const rowUnit = 122
  const rows = Math.max(personas.length, campaigns.length, 1)
  const contentH = rows * rowUnit
  const H = contentTop + contentH + 28

  const personaH = 82, campH = 104, hubH = 84
  const xHub = 24, wHub = 128
  const xPersona = 232, wPersona = 196
  const xCamp = 520, wCamp = 300
  const xLink = 952, wLink = 212

  // evenly distribute `count` nodes of height `nh` down the content band
  const slotTop = (count, i, nh) => {
    const slot = contentH / Math.max(count, 1)
    return contentTop + i * slot + (slot - nh) / 2
  }
  const midY = contentTop + contentH / 2
  const personaCY = (i) => slotTop(personas.length, i, personaH) + personaH / 2
  const campTop = (j) => slotTop(campaigns.length, j, campH)
  const campCY = (j) => campTop(j) + campH / 2

  const personaIdx = Object.fromEntries(personas.map((p, i) => [p.persona, i]))
  const campIdx = Object.fromEntries(campaigns.map((c, j) => [String(c.campaign_id), j]))

  const edgeActive = (persona, cid) => {
    if (!hover) return true
    if (hover.type === 'persona') return hover.id === persona
    if (hover.type === 'campaign') return hover.id === String(cid)
    return true
  }
  const personaDim = (name) => hover && !(hover.type === 'persona' && hover.id === name)
    && !edges.some((e) => e.persona === name && hover.type === 'campaign' && hover.id === String(e.campaign_id))
  const campDim = (cid) => hover && !(hover.type === 'campaign' && hover.id === String(cid))
    && !edges.some((e) => String(e.campaign_id) === String(cid) && hover.type === 'persona' && hover.id === e.persona)

  return (
    <div>
      <h1 className="page-title">Orchestration</h1>
      <p className="page-sub">
        How contacts route from a HubSpot list through persona sub-agents into the Bison campaigns
        they actually enroll in (variant campaign first, persona campaign as fallback) and HeyReach (LinkedIn).
      </p>
      <ErrorBanner error={error} />
      {!rollup ? <Spinner label="Loading…" /> : (
        <div className="panel" style={{ overflowX: 'auto' }}>
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ minWidth: 980 }}>
            <defs>
              <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill={BRAND.jade} />
              </marker>
              <marker id="arrowLi" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill={LINKEDIN_BLUE} />
              </marker>
            </defs>

            {/* edges: hub -> persona */}
            {personas.map((p, i) => {
              const color = PERSONA_COLOR[p.persona] || BRAND.muted
              return (
                <path key={`h-${p.persona}`} d={curve(xHub + wHub, midY, xPersona, personaCY(i))}
                  fill="none" stroke={color} strokeWidth="2" markerEnd="url(#arrow)"
                  opacity={personaDim(p.persona) ? 0.12 : 0.6} style={{ transition: 'opacity .15s' }} />
              )
            })}

            {/* edges: persona -> campaign (the real routing) */}
            {edges.map((e, k) => {
              const pi = personaIdx[e.persona]
              const cj = campIdx[String(e.campaign_id)]
              if (pi == null || cj == null) return null
              const color = PERSONA_COLOR[e.persona] || BRAND.muted
              const active = edgeActive(e.persona, e.campaign_id)
              return (
                <path key={`e-${k}`} d={curve(xPersona + wPersona, personaCY(pi), xCamp, campCY(cj))}
                  fill="none" stroke={color} strokeWidth={active ? 2 : 1}
                  markerEnd="url(#arrow)" opacity={active ? 0.55 : 0.08}
                  style={{ transition: 'opacity .15s' }} />
              )
            })}

            {/* edges: campaign -> LinkedIn (second channel every contact can feed) */}
            {linkedin && campaigns.map((c, j) => (
              <path key={`l-${c.campaign_id}`} d={curve(xCamp + wCamp, campCY(j), xLink, midY)}
                fill="none" stroke={LINKEDIN_BLUE} strokeWidth="1.5" markerEnd="url(#arrowLi)"
                strokeDasharray="4 3"
                opacity={hover?.type === 'campaign' && hover.id === String(c.campaign_id) ? 0.8 : (campDim(c.campaign_id) ? 0.06 : 0.22)}
                style={{ transition: 'opacity .15s' }} />
            ))}

            {/* HubSpot source node */}
            <g>
              <rect x={xHub} y={midY - hubH / 2} width={wHub} height={hubH} rx="12" fill="#fff" stroke={BRAND.border} />
              <text x={xHub + wHub / 2} y={midY - 16} textAnchor="middle" fill={BRAND.ink} fontSize="14" fontWeight="700">HubSpot</text>
              <text x={xHub + wHub / 2} y={midY + 4} textAnchor="middle" fill={BRAND.muted} fontSize="11">ICP list</text>
              <text x={xHub + wHub / 2} y={midY + 22} textAnchor="middle" fill={BRAND.muted} fontSize="11">
                {num(personas.reduce((a, p) => a + p.contacts, 0))} contacts
              </text>
            </g>

            {/* LinkedIn / HeyReach channel — a single campaign every persona feeds */}
            {linkedin && (
              <g opacity={hover ? 0.6 : 1} style={{ transition: 'opacity .15s' }}>
                <rect x={xLink} y={midY - 52} width={wLink} height={104} rx="12" fill="#f4f8fd" stroke={LINKEDIN_BLUE} strokeWidth="1.5" />
                <LinkedInIcon x={xLink + 14} y={midY - 38} />
                <text x={xLink + 38} y={midY - 26} fill={BRAND.ink} fontSize="13" fontWeight="700">LinkedIn</text>
                <text x={xLink + 16} y={midY} fill={BRAND.ink} fontSize="13" fontWeight="700">
                  {linkedin.campaign_name || `HeyReach #${linkedin.campaign_id}`}
                </text>
                <text x={xLink + 16} y={midY + 20} fill={BRAND.muted} fontSize="11">HeyReach · #{linkedin.campaign_id}</text>
                <text x={xLink + 16} y={midY + 40} fill={LINKEDIN_BLUE} fontSize="12" fontWeight="700">{num(linkedin.leads)} leads in campaign</text>
              </g>
            )}

            {/* persona / agent nodes */}
            {personas.map((p, i) => {
              const y = slotTop(personas.length, i, personaH)
              const color = PERSONA_COLOR[p.persona] || BRAND.muted
              const enrolled = p.by_status?.enrolled || 0
              return (
                <g key={p.persona} opacity={personaDim(p.persona) ? 0.25 : 1}
                  onMouseEnter={() => setHover({ type: 'persona', id: p.persona })} onMouseLeave={() => setHover(null)}
                  style={{ transition: 'opacity .15s', cursor: 'pointer' }}>
                  <rect x={xPersona} y={y} width={wPersona} height={personaH} rx="12" fill="#fff" stroke={color} strokeWidth="1.5" />
                  <text x={xPersona + 16} y={y + 26} fill={BRAND.ink} fontSize="14" fontWeight="700">{p.persona}</text>
                  <text x={xPersona + 16} y={y + 45} fill={BRAND.muted} fontSize="11">{AGENT[p.persona]}</text>
                  <text x={xPersona + 16} y={y + 66} fill={color} fontSize="12" fontWeight="700">{num(p.contacts)} contacts</text>
                  <text x={xPersona + wPersona - 16} y={y + 66} textAnchor="end" fill={BRAND.muted} fontSize="11">{num(enrolled)} enrolled</text>
                </g>
              )
            })}

            {/* campaign nodes — every campaign in use */}
            {campaigns.map((c, j) => {
              const y = campTop(j)
              const accent = KIND_COLOR[c.kind] || BRAND.muted
              const title = c.campaign_name || (c.campaign_id != null ? `Campaign #${c.campaign_id}` : 'Unrouted')
              const s = c.stats
              return (
                <g key={String(c.campaign_id)} opacity={campDim(c.campaign_id) ? 0.25 : 1}
                  onMouseEnter={() => setHover({ type: 'campaign', id: String(c.campaign_id) })} onMouseLeave={() => setHover(null)}
                  style={{ transition: 'opacity .15s', cursor: 'pointer' }}>
                  <rect x={xCamp} y={y} width={wCamp} height={campH} rx="12" fill="#fbfcfb" stroke={c.kind === 'unrouted' ? BRAND.red : BRAND.border} />
                  <EmailIcon x={xCamp + 16} y={y + 16} color={BRAND.jade} />
                  <text x={xCamp + 40} y={y + 26} fill={BRAND.ink} fontSize="13" fontWeight="700">{title}</text>
                  {c.campaign_id != null && (
                    <text x={xCamp + wCamp - 14} y={y + 26} textAnchor="end" fill={BRAND.muted} fontSize="10">#{c.campaign_id}</text>
                  )}
                  {/* route badge: variant / persona */}
                  <text x={xCamp + 40} y={y + 44} fill={accent} fontSize="10.5" fontWeight="700">
                    {c.kind === 'variant' ? `variant · ${c.label}`
                      : c.kind === 'persona' ? `persona · ${c.label}`
                      : c.kind === 'unrouted' ? 'no campaign configured'
                      : 'campaign'}
                  </text>
                  {/* this pipeline's own contribution */}
                  <text x={xCamp + 16} y={y + 66} fill={BRAND.jadeDeep} fontSize="11" fontWeight="700">
                    {num(c.pipeline_enrolled)} enrolled / {num(c.pipeline_contacts)} routed here
                  </text>
                  {/* full Bison campaign totals — ALL sources, clearly labeled */}
                  {s ? (
                    <text x={xCamp + 16} y={y + 86} fill={BRAND.muted} fontSize="11">
                      campaign total: {num(s.total_leads_contacted)} contacted · {num(s.interested)} interested (all sources)
                    </text>
                  ) : (
                    <text x={xCamp + 16} y={y + 86} fill={BRAND.muted} fontSize="11">
                      no cached campaign stats — refresh Analytics
                    </text>
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
            <span className="muted">
              · <b>routed here</b> / <b>enrolled</b> count this pipeline's contacts; <b>campaign total</b> is the full
              Bison campaign (every lead from any source). Hover a node to isolate its routes.
            </span>
          </div>
        </div>
      )}
    </div>
  )
}
