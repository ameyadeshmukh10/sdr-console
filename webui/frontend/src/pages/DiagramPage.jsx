import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num, pct, EmailIcon, LinkedInIcon, LINKEDIN_BLUE } from '../components/ui.jsx'
import { BRAND, PERSONA_COLORS } from '../theme.js'

// Pillar 2 — See: orchestration topology. HubSpot list -> persona routing ->
// persona sub-agents -> EVERY Bison campaign contacts actually route into (per
// contact: variant campaign first, persona campaign fallback) + HeyReach (LinkedIn).
const PERSONA_COLOR = PERSONA_COLORS
const AGENT = {
  'sales-leadership': 'sdr-sales-leadership',
  'revops': 'sdr-revops',
  'partnerships': 'sdr-partnerships',
  'sdr-bdr': 'sdr-sdr-bdr-leadership',
}
// Accent per campaign node by how it's routed.
const KIND_COLOR = { variant: BRAND.jade, persona: BRAND.teal, unrouted: BRAND.red, campaign: BRAND.muted }

// horizontal-tangent bezier between two points (left edge -> right edge of nodes)
function curve(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`
}

export default function DiagramPage() {
  const [rollup, setRollup] = useState(null)
  const [error, setError] = useState(null)
  const [hover, setHover] = useState(null)   // {type:'persona'|'campaign', id}
  const [unenroll, setUnenroll] = useState(null)  // unenrollment checker status
  const [runBusy, setRunBusy] = useState(false)   // one global run at a time
  const [runMsg, setRunMsg] = useState(null)

  useEffect(() => {
    api.rollup().then(setRollup).catch((e) => setError(e.message))
    // Non-fatal: the diagram renders fine without it (e.g. an older backend).
    api.unenrollStatus().then(setUnenroll).catch(() => {})
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
  const gateH = 64
  const gateY = contentTop + contentH + 20
  const H = contentTop + contentH + 28 + (unenroll ? gateH + 24 : 0)

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

  // ---- unenrollment gate (safety bar under both outbound channels) ---------
  const gateRule = unenroll?.rules?.[0]
  const gateCounts = gateRule?.counts?.available ? gateRule.counts : null
  const gateStopped = gateCounts
    ? Object.values(gateCounts.by_channel_action || {}).reduce((a, c) => a + (c?.stopped || 0), 0)
    : null
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

  return (
    <div>
      <h1 className="page-title">Orchestration</h1>
      <p className="page-sub">
        How contacts route from a HubSpot list through persona sub-agents into the email campaigns
        they actually enroll in (variant campaign first, persona campaign as fallback) and LinkedIn.
        The unenrollment checker below sweeps both channels and stops anyone RevOps has tagged
        do-not-contact.
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
                  {linkedin.campaign_name || `LinkedIn #${linkedin.campaign_id}`}
                </text>
                <text x={xLink + 16} y={midY + 20} fill={BRAND.muted} fontSize="11">LinkedIn · #{linkedin.campaign_id}</text>
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

            {/* safety gate — the unenrollment checker sits under both outbound channels */}
            {unenroll && (
              <g opacity={hover ? 0.6 : 1} style={{ transition: 'opacity .15s' }}>
                <path d={`M${xCamp + wCamp / 2},${gateY} L${xCamp + wCamp / 2},${gateY - 14}`}
                  fill="none" stroke={BRAND.red} strokeWidth="1.5" strokeDasharray="4 3" opacity="0.5" />
                <path d={`M${xLink + wLink / 2},${gateY} L${xLink + wLink / 2},${gateY - 14}`}
                  fill="none" stroke={BRAND.red} strokeWidth="1.5" strokeDasharray="4 3" opacity="0.5" />
                <rect x={xCamp} y={gateY} width={xLink + wLink - xCamp} height={gateH} rx="12"
                  fill="#fffdf7" stroke={BRAND.red} strokeWidth="1.5" strokeDasharray="6 3" />
                <text x={xCamp + 16} y={gateY + 26} fill={BRAND.ink} fontSize="13" fontWeight="700">Unenrollment checker</text>
                <text x={xCamp + 16} y={gateY + 45} fill={BRAND.muted} fontSize="10.5">{gateSub}</text>
                {gateCounts && (
                  <text x={xLink + wLink - 16} y={gateY + 26} textAnchor="end" fill={BRAND.red} fontSize="12" fontWeight="700">
                    {num(gateStopped)} stopped · {num(gateCounts.contacts)} flagged
                  </text>
                )}
              </g>
            )}
          </svg>

          <div className="row" style={{ gap: 18, marginTop: 10, fontSize: 12, alignItems: 'center', flexWrap: 'wrap' }}>
            <span className="muted">Channels:</span>
            <span className="row" style={{ gap: 6, alignItems: 'center' }}>
              <svg width="18" height="14"><EmailIcon x={1} y={1} color={BRAND.jade} /></svg> Email
            </span>
            <span className="row" style={{ gap: 6, alignItems: 'center' }}>
              <svg width="18" height="16"><LinkedInIcon x={1} y={0} /></svg> LinkedIn
            </span>
            <span className="muted">
              · <b>routed here</b> / <b>enrolled</b> count this pipeline's contacts; <b>campaign total</b> is the full
              email campaign (every lead from any source). Hover a node to isolate its routes.
            </span>
          </div>
        </div>
      )}

      {/* Suppression rules — one data-driven card per rule the checker enforces. */}
      {unenroll && (
        <>
          <div className="section-h" style={{ marginTop: 18 }}>Unenrollment & suppression rules</div>
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
