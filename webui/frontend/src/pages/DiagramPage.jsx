import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { Spinner, ErrorBanner, EmailIcon, LinkedInIcon, LINKEDIN_BLUE } from '../components/ui.jsx'
import { BRAND, PERSONA_COLORS } from '../theme.js'
import {
  SectionFrame, PipelineSection, IcpFilterSection, PersonaAgentsSection,
  SequencingSection, KnowledgeSection, GuardrailsSection, SignalsSection,
} from '../components/OrchestrationSections.jsx'
import Connectors from '../components/Connectors.jsx'
import ConfigChat from '../components/ConfigChat.jsx'

// Pillar 2 — See: how the pipeline WORKS (not how much it processed).
// HubSpot -> ICP filter -> agent orchestrator -> persona agents -> Email + LinkedIn,
// fed by the Signal Intelligence / Knowledge / Guardrails layers, gated by the
// unenrollment checker. Numbers live on other pages; every node opens its
// "under the hood" section below, populated live from the repo config.

const PERSONAS = ['sales-leadership', 'revops', 'partnerships', 'sdr-bdr']
const AGENT = {
  'sales-leadership': 'sdr-sales-leadership',
  'revops': 'sdr-revops',
  'partnerships': 'sdr-partnerships',
  'sdr-bdr': 'sdr-sdr-bdr-leadership',
}

// horizontal-tangent bezier (left edge -> right edge of nodes)
function curve(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`
}
// vertical-tangent bezier (layer boxes reaching up into the flow)
function vcurve(x1, y1, x2, y2) {
  const my = (y1 + y2) / 2
  return `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`
}

// ---- static layout ---------------------------------------------------------
const W = 1200
const TOP = 64, SLOT = 92
const BAND = PERSONAS.length * SLOT              // persona band height
const MID = TOP + BAND / 2                       // 248
const HUB = { x: 24, w: 150, h: 76 }
const ICP = { x: 214, w: 168, h: 76 }
const ORCH = { x: 422, w: 180, h: 90 }
const PER = { x: 662, w: 212, h: 64 }
const CHAN = { x: 944, w: 220, h: 76 }
const EMAIL_Y = MID - 96, LINK_Y = MID + 20
const LAYER_Y = TOP + BAND + 36                  // 468
const LAYER_H = 68
const LAYERS = [
  { id: 'signals', x: 214, w: 220, title: 'Signal Intelligence', sub: 'technographic + hiring scans' },
  { id: 'knowledge', x: 444, w: 220, title: 'Knowledge', sub: 'offer · CTAs · email rules' },
  { id: 'guardrails', x: 674, w: 220, title: 'Guardrails', sub: 'lint gate before enrollment' },
]
const GATE_Y = LAYER_Y + LAYER_H + 36            // 572
const GATE_H = 64
const H = GATE_Y + GATE_H + 20

const personaY = (i) => TOP + i * SLOT + (SLOT - PER.h) / 2
const personaCY = (i) => personaY(i) + PER.h / 2

export default function DiagramPage() {
  const [config, setConfig] = useState(null)
  const [error, setError] = useState(null)
  const [hover, setHover] = useState(null)        // persona id being hovered
  // Under-the-hood is a tab strip, not a stack of accordions: one section on
  // screen at a time, all of them reachable in one click from anywhere.
  const [tab, setTab] = useState('pipeline')
  const [conn, setConnectors] = useState(null)    // connector inventory
  const [connError, setConnError] = useState(null)
  const [cfgScopes, setCfgScopes] = useState(null)  // chat-editable config scopes
  const [unenroll, setUnenroll] = useState(null)  // unenrollment checker status
  const sectionRefs = useRef({})
  const navigate = useNavigate()

  useEffect(() => {
    api.orchestrationConfig().then(setConfig).catch((e) => setError(e.message))
    api.connectors().then(setConnectors).catch((e) => setConnError(e.message))
    // Non-fatal: sections still render read-only if this backend lacks the endpoint.
    api.configScopes().then(setCfgScopes).catch(() => {})
    // Non-fatal: the diagram renders fine without it (e.g. an older backend).
    api.unenrollStatus().then(setUnenroll).catch(() => {})
  }, [])

  // Clicking a node in the diagram selects that tab and brings the strip into
  // view, so the diagram stays the navigation for the config below it. The safety
  // gate is the exception — running it lives on Pipeline, so send them there.
  function openSection(id) {
    if (id === 'unenroll') {
      navigate('/pipeline')
      return
    }
    setTab(id)
    setTimeout(() => sectionRefs.current.tabs?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 60)
  }

  const dim = (p) => hover && hover !== p

  const node = (id) => ({
    onClick: () => openSection(id),
    style: { cursor: 'pointer' },
  })

  // `controls` / `sources` give every section the same frame: what it decides, and
  // the exact files to edit to change that decision.
  const sections = [
    { id: 'pipeline', title: 'Pipeline stages & agent routing', tab: 'Pipeline', sub: 'sdr-pipeline SKILL.md · live',
      C: PipelineSection, data: config?.pipeline,
      controls: 'the order of pipeline stages, and which persona agent each contact is routed to.',
      sources: ['.claude/skills/sdr-pipeline/SKILL.md'] },
    { id: 'icp', title: 'ICP filter — who gets written to', tab: 'ICP filter', sub: 'buyer_group.py · live',
      C: IcpFilterSection, data: config?.icp_filter,
      controls: 'which job titles count as ICP and which persona they map to. Non-matches are skipped at pull time.',
      sources: ['.claude/skills/ai-sdr/scripts/buyer_group.py'] },
    { id: 'personas', title: 'Persona agents', tab: 'Personas', sub: '.claude/agents · live',
      C: PersonaAgentsSection, data: config?.personas,
      controls: 'the pain, outcome, CTA set and tone each persona agent writes with.',
      sources: ['.claude/agents/sdr-sales-leadership.md', '.claude/agents/sdr-revops.md',
                '.claude/agents/sdr-partnerships.md', '.claude/agents/sdr-sdr-bdr-leadership.md'] },
    { id: 'sequencing', title: 'Sequencing & CTA offers', tab: 'Sequencing', sub: 'icp-email.md · cta-offers.md · live',
      C: SequencingSection, data: config?.sequencing,
      controls: 'the job of each of the 4 email touches and 3 LinkedIn touches, and the offer library the CTAs draw from.',
      sources: ['.claude/skills/ai-sdr/knowledge/icp-email.md',
                '.claude/skills/ai-sdr/knowledge/cta-offers.md'] },
    { id: 'knowledge', title: 'Knowledge base', tab: 'Knowledge', sub: 'offer.md · live',
      C: KnowledgeSection, data: config?.knowledge,
      controls: 'the product story and the only proof points the copy is allowed to cite.',
      sources: ['.claude/skills/ai-sdr/knowledge/offer.md'] },
    { id: 'guardrails', title: 'Guardrails', tab: 'Guardrails', sub: 'lint_sequence.py · icp-email.md · live',
      C: GuardrailsSection, data: config?.guardrails,
      controls: 'the checks every email must pass before enrollment, and the writing rules agents are held to.',
      sources: ['.claude/skills/ai-sdr/scripts/lint_sequence.py'] },
    { id: 'signals', title: 'Signal intelligence', tab: 'Signals', sub: 'technographics + hiring config · live',
      C: SignalsSection, data: config?.signals,
      controls: 'which account signals are detected, how long they are cached, whether they write back to HubSpot, and how each one changes the copy.',
      sources: ['.claude/skills/sdr-pipeline/scripts/tech_signals.py',
                '.claude/skills/sdr-pipeline/scripts/hiring_signals.py'],
      editNote: 'the detection scope lives in the two scripts above; the on/off, cache-window and '
        + 'write-back settings are environment variables (Railway service variables in prod, .env locally) '
        + 'and take effect on the next scan without a code change.' },
    // Last tab: not repo config like the others, so it renders its own panel and
    // opts out of the shared frame (no source files to point at).
    { id: 'connectors', title: 'Connected systems', sub: 'detected from configuration', tab: 'Connectors',
      C: () => <Connectors data={conn} error={connError} bare />, data: null,
      frameless: true },
  ]
  const failedSections = Object.keys(config?.errors || {})

  return (
    <div>
      <h1 className="page-title">Setup</h1>
      <p className="page-sub">
        How the pipeline works: HubSpot contacts pass the ICP filter, the orchestrator routes each
        to its persona agent, and copy goes out over email and LinkedIn — grounded in the knowledge
        base, shaped by signal intelligence, gated by the linter and the unenrollment checker.
        Click any node for what's under the hood.
      </p>
      <ErrorBanner error={error} />

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

          {/* flow edges: hub -> ICP -> orchestrator */}
          <path d={curve(HUB.x + HUB.w, MID, ICP.x, MID)} fill="none" stroke={BRAND.jade}
            strokeWidth="2" markerEnd="url(#arrow)" opacity="0.6" />
          <path d={curve(ICP.x + ICP.w, MID, ORCH.x, MID)} fill="none" stroke={BRAND.jade}
            strokeWidth="2" markerEnd="url(#arrow)" opacity="0.6" />

          {/* orchestrator -> personas (fan-out) */}
          {PERSONAS.map((p, i) => (
            <path key={`o-${p}`} d={curve(ORCH.x + ORCH.w, MID, PER.x, personaCY(i))}
              fill="none" stroke={PERSONA_COLORS[p]} strokeWidth="2" markerEnd="url(#arrow)"
              opacity={dim(p) ? 0.1 : 0.6} style={{ transition: 'opacity .15s' }} />
          ))}

          {/* personas -> Email (solid) and -> LinkedIn (dashed) */}
          {PERSONAS.map((p, i) => (
            <g key={`c-${p}`}>
              <path d={curve(PER.x + PER.w, personaCY(i), CHAN.x, EMAIL_Y + CHAN.h / 2)}
                fill="none" stroke={BRAND.jade} strokeWidth="1.8" markerEnd="url(#arrow)"
                opacity={dim(p) ? 0.08 : 0.45} style={{ transition: 'opacity .15s' }} />
              <path d={curve(PER.x + PER.w, personaCY(i), CHAN.x, LINK_Y + CHAN.h / 2)}
                fill="none" stroke={LINKEDIN_BLUE} strokeWidth="1.5" strokeDasharray="4 3"
                markerEnd="url(#arrowLi)" opacity={dim(p) ? 0.06 : 0.3}
                style={{ transition: 'opacity .15s' }} />
            </g>
          ))}

          {/* feeding-layer edges (dashed, reaching up into the flow) */}
          <path d={vcurve(LAYERS[0].x + LAYERS[0].w / 2, LAYER_Y, ORCH.x + ORCH.w / 2, MID + ORCH.h / 2)}
            fill="none" stroke={BRAND.teal} strokeWidth="1.5" strokeDasharray="4 3" opacity="0.45" />
          <path d={vcurve(LAYERS[1].x + LAYERS[1].w / 2, LAYER_Y, PER.x + PER.w / 2, TOP + BAND)}
            fill="none" stroke={BRAND.teal} strokeWidth="1.5" strokeDasharray="4 3" opacity="0.45" />
          <path d={vcurve(LAYERS[2].x + LAYERS[2].w / 2, LAYER_Y, CHAN.x - 20, MID)}
            fill="none" stroke={BRAND.teal} strokeWidth="1.5" strokeDasharray="4 3" opacity="0.45" />

          {/* HubSpot source */}
          <g {...node('pipeline')}>
            <rect x={HUB.x} y={MID - HUB.h / 2} width={HUB.w} height={HUB.h} rx="12" fill="#fff" stroke={BRAND.border} />
            <text x={HUB.x + HUB.w / 2} y={MID - 8} textAnchor="middle" fill={BRAND.ink} fontSize="14" fontWeight="700">HubSpot</text>
            <text x={HUB.x + HUB.w / 2} y={MID + 12} textAnchor="middle" fill={BRAND.muted} fontSize="11">ICP contact list</text>
          </g>

          {/* ICP filter */}
          <g {...node('icp')}>
            <rect x={ICP.x} y={MID - ICP.h / 2} width={ICP.w} height={ICP.h} rx="12" fill="#fff" stroke={BRAND.border} />
            <text x={ICP.x + ICP.w / 2} y={MID - 8} textAnchor="middle" fill={BRAND.ink} fontSize="14" fontWeight="700">ICP filter</text>
            <text x={ICP.x + ICP.w / 2} y={MID + 12} textAnchor="middle" fill={BRAND.muted} fontSize="11">title → buyer group</text>
          </g>

          {/* Agent orchestrator */}
          <g {...node('pipeline')}>
            <rect x={ORCH.x} y={MID - ORCH.h / 2} width={ORCH.w} height={ORCH.h} rx="12" fill="#fff" stroke={BRAND.jade} strokeWidth="1.5" />
            <text x={ORCH.x + ORCH.w / 2} y={MID - 12} textAnchor="middle" fill={BRAND.ink} fontSize="14" fontWeight="700">Agent</text>
            <text x={ORCH.x + ORCH.w / 2} y={MID + 6} textAnchor="middle" fill={BRAND.ink} fontSize="14" fontWeight="700">Orchestrator</text>
            <text x={ORCH.x + ORCH.w / 2} y={MID + 26} textAnchor="middle" fill={BRAND.muted} fontSize="11">routes by persona</text>
          </g>

          {/* persona agents */}
          {PERSONAS.map((p, i) => (
            <g key={p} opacity={dim(p) ? 0.3 : 1} {...node('personas')}
              onMouseEnter={() => setHover(p)} onMouseLeave={() => setHover(null)}
              style={{ transition: 'opacity .15s', cursor: 'pointer' }}>
              <rect x={PER.x} y={personaY(i)} width={PER.w} height={PER.h} rx="12" fill="#fff"
                stroke={PERSONA_COLORS[p]} strokeWidth="1.5" />
              <text x={PER.x + 16} y={personaY(i) + 26} fill={BRAND.ink} fontSize="14" fontWeight="700">{p}</text>
              <text x={PER.x + 16} y={personaY(i) + 46} fill={BRAND.muted} fontSize="11">{AGENT[p]}</text>
            </g>
          ))}

          {/* channels */}
          <g {...node('sequencing')}>
            <rect x={CHAN.x} y={EMAIL_Y} width={CHAN.w} height={CHAN.h} rx="12" fill="#fbfcfb" stroke={BRAND.jade} strokeWidth="1.5" />
            <EmailIcon x={CHAN.x + 16} y={EMAIL_Y + 16} color={BRAND.jade} />
            <text x={CHAN.x + 42} y={EMAIL_Y + 28} fill={BRAND.ink} fontSize="14" fontWeight="700">Email</text>
            <text x={CHAN.x + 16} y={EMAIL_Y + 54} fill={BRAND.muted} fontSize="11">4-touch sequence · Email Bison</text>
          </g>
          <g {...node('sequencing')}>
            <rect x={CHAN.x} y={LINK_Y} width={CHAN.w} height={CHAN.h} rx="12" fill="#f4f8fd" stroke={LINKEDIN_BLUE} strokeWidth="1.5" />
            <LinkedInIcon x={CHAN.x + 16} y={LINK_Y + 14} />
            <text x={CHAN.x + 42} y={LINK_Y + 28} fill={BRAND.ink} fontSize="14" fontWeight="700">LinkedIn</text>
            <text x={CHAN.x + 16} y={LINK_Y + 54} fill={BRAND.muted} fontSize="11">3 touches · HeyReach</text>
          </g>

          {/* feeding layers */}
          {LAYERS.map((l) => (
            <g key={l.id} {...node(l.id)}>
              <rect x={l.x} y={LAYER_Y} width={l.w} height={LAYER_H} rx="12"
                fill="rgba(51,182,144,0.06)" stroke={BRAND.teal} strokeWidth="1.5" />
              <text x={l.x + l.w / 2} y={LAYER_Y + 28} textAnchor="middle" fill={BRAND.ink} fontSize="13" fontWeight="700">{l.title}</text>
              <text x={l.x + l.w / 2} y={LAYER_Y + 48} textAnchor="middle" fill={BRAND.muted} fontSize="10.5">{l.sub}</text>
            </g>
          ))}

          {/* safety gate — the unenrollment checker sits under both outbound channels */}
          {unenroll && (
            <g {...node('unenroll')}>
              <path d={`M${CHAN.x + CHAN.w / 2},${LINK_Y + CHAN.h} L${CHAN.x + CHAN.w / 2},${GATE_Y}`}
                fill="none" stroke={BRAND.red} strokeWidth="1.5" strokeDasharray="4 3" opacity="0.5" />
              <rect x={CHAN.x} y={GATE_Y} width={CHAN.w} height={GATE_H} rx="12"
                fill="#fffdf7" stroke={BRAND.red} strokeWidth="1.5" strokeDasharray="6 3" />
              <text x={CHAN.x + 16} y={GATE_Y + 26} fill={BRAND.ink} fontSize="13" fontWeight="700">Unenrollment checker</text>
              <text x={CHAN.x + 16} y={GATE_Y + 45} fill={BRAND.muted} fontSize="10.5">safety gate · run it on Pipeline</text>
            </g>
          )}
        </svg>

        <div className="row" style={{ gap: 18, marginTop: 10, fontSize: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <span className="row" style={{ gap: 6, alignItems: 'center' }}>
            <svg width="18" height="14"><EmailIcon x={1} y={1} color={BRAND.jade} /></svg> Email
          </span>
          <span className="row" style={{ gap: 6, alignItems: 'center' }}>
            <svg width="18" height="16"><LinkedInIcon x={1} y={0} /></svg> LinkedIn
          </span>
          <span className="muted">
            · dashed = feeding layer / safety gate · click any node for what's under the hood
          </span>
        </div>
      </div>

      {/* ---- under the hood: live config sections -------------------------- */}
      <div className="section-h" style={{ marginTop: 18 }}>Under the hood</div>
      <p className="muted" style={{ fontSize: 12.5, marginTop: -4, maxWidth: 700 }}>
        Every section reads the live config from the repo sources named on its right, and
        ends with how to change it. Nothing here is hand-maintained copy.
      </p>
      {!config && !error && <Spinner label="Loading pipeline config…" />}
      {failedSections.length > 0 && (
        <p className="muted" style={{ fontSize: 12 }}>
          Some sections could not be parsed from the repo sources: {failedSections.join(', ')}.
        </p>
      )}
      {config && (() => {
        const active = sections.find((x) => x.id === tab) || sections[0]
        const { title, sub, C, data, controls, sources, editNote } = active
        return (
          <>
            <div className="uth-tabs" ref={(el) => { sectionRefs.current.tabs = el }}
              role="tablist" aria-label="Configuration sections">
              {sections.map((sc) => (
                <button key={sc.id} role="tab" aria-selected={sc.id === active.id}
                  className={'uth-tab' + (sc.id === active.id ? ' active' : '')}
                  onClick={() => setTab(sc.id)}>
                  {sc.tab}
                </button>
              ))}
            </div>
            <div className="panel uth-panel" role="tabpanel">
              <div className="row between" style={{ alignItems: 'flex-start', gap: 12, marginBottom: 4 }}>
                <div className="section-h" style={{ marginTop: 0, marginBottom: 0 }}>{title}</div>
                <span className="muted" style={{ fontSize: 11.5, whiteSpace: 'nowrap' }}>{sub}</span>
              </div>
              {active.frameless
                ? <C data={data} />
                : (
                  <SectionFrame controls={controls} sources={sources} editNote={editNote}>
                    <C data={data} />
                  </SectionFrame>
                )}
              {cfgScopes && (
                <ConfigChat
                  scope={active.id}
                  meta={cfgScopes.scopes?.find((x) => x.id === active.id)}
                  persistence={cfgScopes.persistence}
                  available={cfgScopes.available}
                  history={cfgScopes.history}
                  onApplied={() => {
                    // Config changed on disk — re-read both the rendered config and
                    // the audit history so the section reflects the new state.
                    api.orchestrationConfig().then(setConfig).catch(() => {})
                    api.configScopes().then(setCfgScopes).catch(() => {})
                  }}
                />
              )}
            </div>
          </>
        )
      })()}

    </div>
  )
}
