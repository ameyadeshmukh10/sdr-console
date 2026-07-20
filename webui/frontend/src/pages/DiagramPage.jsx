import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num, EmailIcon, LinkedInIcon, LINKEDIN_BLUE } from '../components/ui.jsx'
import { BRAND, PERSONA_COLORS } from '../theme.js'
import {
  SectionCard, PipelineSection, IcpFilterSection, PersonaAgentsSection,
  SequencingSection, KnowledgeSection, GuardrailsSection, SignalsSection,
} from '../components/OrchestrationSections.jsx'

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
  const [expanded, setExpanded] = useState(() => new Set())
  const [unenroll, setUnenroll] = useState(null)  // unenrollment checker status
  const [runBusy, setRunBusy] = useState(false)   // one global run at a time
  const [runMsg, setRunMsg] = useState(null)
  const sectionRefs = useRef({})

  useEffect(() => {
    api.orchestrationConfig().then(setConfig).catch((e) => setError(e.message))
    // Non-fatal: the diagram renders fine without it (e.g. an older backend).
    api.unenrollStatus().then(setUnenroll).catch(() => {})
  }, [])

  function toggleSection(id) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function openSection(id) {
    if (id === 'unenroll') {
      sectionRefs.current.unenroll?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
    setExpanded((prev) => new Set(prev).add(id))
    // let the section body mount before scrolling to it
    setTimeout(() => sectionRefs.current[id]?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 60)
  }

  const dim = (p) => hover && hover !== p

  // ---- unenrollment gate (safety bar under both outbound channels) ---------
  const gateRule = unenroll?.rules?.[0]
  const gateSub = unenroll
    ? [gateRule?.description?.replace(/\.\s*$/, ''), `every ${unenroll.interval_minutes}m`]
        .filter(Boolean).join(' · ') + (unenroll.enabled === false ? ' · sweeper disabled' : '')
    : ''

  // Kick a sweep (or dry run), then poll until the global run flag clears and
  // refresh the rule cards — same pattern as the Analytics attribution sync.
  async function runCheck(dryRun) {
    setRunBusy(true)
    setRunMsg(dryRun ? 'Starting dry run…' : 'Starting unenrollment check…')
    try {
      await api.unenrollRun({ dry_run: !!dryRun })
    } catch (e) {
      // 409 = a check is already running (e.g. the background sweeper) — keep polling it.
      if (e.status !== 409) {
        setRunMsg(`Unenrollment check failed to start: ${e.message}`)
        setRunBusy(false)
        return
      }
    }
    setRunMsg('Unenrollment check running — sweeping flagged contacts across both channels…')
    for (let i = 0; i < 120; i++) {          // up to ~10 min
      await new Promise((r) => setTimeout(r, 5000))
      try {
        const s = await api.unenrollStatus()
        if (s.running && s.progress) setRunMsg(`Running — ${s.progress}`)
        if (!s.running) {
          setUnenroll(s)
          if (dryRun) {
            // Dry runs never touch last_run — their summary comes via last_result.
            const d = s.last_result
            setRunMsg(d && typeof d === 'object' && d.dry_run
              ? (d.ok === false
                  ? `Dry run failed: ${d.error || (d.errors || []).join('; ') || 'unknown'}`
                  : `Dry run complete — ${num(d.checked || 0)} checked, `
                    + `${num((d.bison?.stopped || 0) + (d.heyreach?.stopped || 0))} would be stopped. No changes made.`)
              : 'Dry run complete — no changes made.')
          } else {
            const lr = s.rules?.[0]?.last_run
            const sum = lr?.summary
            setRunMsg(lr?.ok === false
              ? `Unenrollment check finished with an error: ${typeof sum === 'string' ? sum : (sum?.errors?.length ? sum.errors.join('; ') : (sum?.error || 'unknown'))}`
              : 'Check complete.')
          }
          setRunBusy(false)
          return
        }
      } catch { /* transient — keep polling */ }
    }
    setRunMsg('Unenrollment check is still running — refresh the page later.')
    setRunBusy(false)
  }

  const node = (id) => ({
    onClick: () => openSection(id),
    style: { cursor: 'pointer' },
  })

  const sections = [
    { id: 'pipeline', title: 'Pipeline stages & agent routing', sub: 'sdr-pipeline SKILL.md · live', C: PipelineSection, data: config?.pipeline },
    { id: 'icp', title: 'ICP filter — who gets written to', sub: 'buyer_group.py · live', C: IcpFilterSection, data: config?.icp_filter },
    { id: 'personas', title: 'Persona agents', sub: '.claude/agents · live', C: PersonaAgentsSection, data: config?.personas },
    { id: 'sequencing', title: 'Sequencing & CTA offers', sub: 'icp-email.md · cta-offers.md · live', C: SequencingSection, data: config?.sequencing },
    { id: 'knowledge', title: 'Knowledge base', sub: 'offer.md · live', C: KnowledgeSection, data: config?.knowledge },
    { id: 'guardrails', title: 'Guardrails', sub: 'lint_sequence.py · icp-email.md · live', C: GuardrailsSection, data: config?.guardrails },
    { id: 'signals', title: 'Signal intelligence', sub: 'technographics + hiring config · live', C: SignalsSection, data: config?.signals },
  ]
  const failedSections = Object.keys(config?.errors || {})

  return (
    <div>
      <h1 className="page-title">Orchestration</h1>
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
              <text x={CHAN.x + 16} y={GATE_Y + 45} fill={BRAND.muted} fontSize="10.5">safety gate · details below</text>
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
      {!config && !error && <Spinner label="Loading pipeline config…" />}
      {failedSections.length > 0 && (
        <p className="muted" style={{ fontSize: 12 }}>
          Some sections could not be parsed from the repo sources: {failedSections.join(', ')}.
        </p>
      )}
      {config && sections.map(({ id, title, sub, C, data }) => (
        <SectionCard key={id} id={id} title={title} sub={sub} open={expanded.has(id)}
          onToggle={toggleSection} innerRef={(el) => { sectionRefs.current[id] = el }}>
          <C data={data} />
        </SectionCard>
      ))}

      {/* Suppression rules — one data-driven card per rule the checker enforces. */}
      {unenroll && (
        <>
          <div className="section-h" style={{ marginTop: 18 }}
            ref={(el) => { sectionRefs.current.unenroll = el }}>
            Unenrollment & suppression rules
          </div>
          {(unenroll.rules || []).map((r) => (
            <RuleCard key={r.id} rule={r} busy={runBusy} msg={runMsg} onRun={runCheck}
              running={!!unenroll.running} progress={unenroll.progress} />
          ))}
        </>
      )}
    </div>
  )
}

// Human line for a rule's last sweep — `last_run` may be null/{} (never ran) and
// `summary` may be a raw string when the script output couldn't be parsed.
function lastRunLine(lastRun) {
  if (!lastRun || !lastRun.at) return 'No run yet — first sweep runs ~2 min after deploy.'
  const when = new Date(lastRun.at).toLocaleString()
  const s = lastRun.summary
  if (typeof s === 'string') return `Last run ${when} — ${lastRun.ok === false ? 'error' : 'ok'} · ${s}`
  if (lastRun.ok === false) {
    // A fatal sweep has {error} (singular); a completed-with-failures one has {errors}.
    const why = s?.errors?.length ? s.errors.join('; ') : (s?.error || 'sweep failed')
    return `Last run ${when} — error · ${why}`
  }
  const stopped = (s?.bison?.stopped || 0) + (s?.heyreach?.stopped || 0)
  const detail = s ? ` · ${num(s.checked || 0)} checked, ${num(stopped)} stopped` : ''
  return `Last run ${when} — ok${detail}`
}

// One suppression rule. Fully data-driven from the payload — more rules will
// exist over time and must render here without code changes.
function RuleCard({ rule, busy, msg, onRun, running, progress }) {
  const counts = rule.counts?.available ? rule.counts : null
  const byChan = counts?.by_channel_action || {}
  const chips = [
    { label: 'Email', configured: !!rule.channels?.bison?.configured },
    { label: 'LinkedIn', configured: !!rule.channels?.heyreach?.configured },
  ]
  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <span style={{ fontWeight: 700, fontSize: 15 }}>{rule.name}</span>
        <span className="badge" style={rule.enabled
          ? { color: 'var(--green)', borderColor: 'var(--green)', background: 'rgba(28, 130, 110, 0.08)' }
          : { color: 'var(--muted)' }}>
          {rule.enabled ? 'enabled' : 'disabled'}
        </span>
        {chips.map((c) => (
          <span key={c.label} className="badge" style={c.configured ? undefined : { color: 'var(--muted)' }}>
            {c.label} {c.configured ? '✓ configured' : '— not configured'}
          </span>
        ))}
      </div>
      <p className="muted" style={{ fontSize: 12.5, margin: '8px 0 14px' }}>{rule.description}</p>
      {counts ? (
        <div className="grid stat-grid" style={{ marginBottom: 14 }}>
          <Stat label="Contacts flagged" value={num(counts.contacts)} />
          <Stat label="Stopped — email" value={num(byChan.bison?.stopped || 0)} />
          <Stat label="Stopped — LinkedIn" value={num(byChan.heyreach?.stopped || 0)} />
          <Stat label="Failed" value={num(counts.failed || 0)} tone={(counts.failed || 0) > 0 ? 'bad' : 'good'} />
        </div>
      ) : (
        <p className="muted" style={{ fontSize: 12, margin: '0 0 14px' }}>No sweep results recorded yet.</p>
      )}
      <p className="muted" style={{ fontSize: 12, margin: '0 0 14px' }}>{lastRunLine(rule.last_run)}</p>
      <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="sm" onClick={() => onRun(false)} disabled={busy}>
          {busy ? <Spinner label="Running…" /> : 'Run now'}
        </button>
        <button className="ghost sm" onClick={() => onRun(true)} disabled={busy}>Dry run</button>
      </div>
      {msg && <p className="muted" style={{ fontSize: 12, margin: '10px 0 0' }}>{msg}</p>}
      {!msg && running && (
        // The background sweeper is mid-run (nobody clicked anything here) —
        // say so instead of looking idle. Clicking Run now attaches to it.
        <p className="muted" style={{ fontSize: 12, margin: '10px 0 0' }}>
          A sweep is running now{progress ? ` — ${progress}` : '…'}
        </p>
      )}
    </div>
  )
}
