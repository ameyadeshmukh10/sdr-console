import { useState } from 'react'
import { Badge } from './ui.jsx'
import { BRAND, PERSONA_COLORS } from '../theme.js'

// "Under the hood" cards for the Orchestration view. Every section renders the
// LIVE config from /api/orchestration/config (agent md files, buyer_group.py,
// signal constant sets) — nothing here is hand-maintained copy. A null section
// (source file missing / parse failure) degrades to a one-line note.

const PLAYBOOK_TONE = {
  sequencing: BRAND.jade,
  intent_abm: BRAND.violet,
  ads: BRAND.amber,
  never_mention: BRAND.red,
}
const PLAYBOOK_LABEL = {
  sequencing: 'sequencing → email 2 play',
  intent_abm: 'intent/ABM → email 3 play',
  ads: 'ad pixels → email 3 play',
  never_mention: 'never mentioned in copy',
}

function Chip({ label, color, strike, title }) {
  return (
    <span className="badge" title={title} style={{
      color: color || 'var(--muted)', borderColor: color || 'var(--border)',
      textDecoration: strike ? 'line-through' : 'none', marginRight: 6, marginBottom: 6,
      display: 'inline-block',
    }}>{label}</span>
  )
}

function ChipRow({ children }) {
  return <div style={{ lineHeight: 1.9, marginTop: 4 }}>{children}</div>
}

function RawPatterns({ rows }) {
  // rows: [{label, raw}]
  const [open, setOpen] = useState(false)
  return (
    <div style={{ marginTop: 8 }}>
      <button className="linklike" onClick={() => setOpen((v) => !v)}>
        {open ? '▾ hide raw patterns' : '▸ show raw patterns'}
      </button>
      {open && (
        <div className="touch" style={{ marginTop: 6 }}>
          {rows.map((r, i) => (
            <div key={i} className="mono" style={{ fontSize: 11, padding: '2px 0', overflowWrap: 'anywhere' }}>
              {r.label && <span className="muted">{r.label}: </span>}{r.raw}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Unavailable({ what }) {
  return <p className="muted" style={{ fontSize: 12 }}>{what} could not be loaded from the repo sources.</p>
}

// Every "under the hood" section wears the same frame, so the view reads as one
// system instead of eight bespoke cards:
//   header  — what this section CONTROLS, plus the files it is read from
//   body    — the section's own rendering
//   footer  — how to change it
// The source chips copy their path on click. Editing is deliberately not offered
// inline: this config lives in versioned repo files that the pipeline agents also
// read, so a form here would fork the truth. Pointing precisely at the file is the
// honest affordance until there's a real config-write path.
export function SectionFrame({ controls, sources = [], editNote, children }) {
  const [copied, setCopied] = useState(null)
  function copy(path) {
    navigator.clipboard?.writeText(path).then(
      () => { setCopied(path); setTimeout(() => setCopied(null), 1200) },
      () => {},
    )
  }
  return (
    <>
      <div className="uth-head">
        <div className="uth-controls">
          {controls && <><b>Controls:</b> {controls}</>}
        </div>
        {sources.length > 0 && (
          <div className="uth-src">
            {sources.map((s) => (
              <button key={s} onClick={() => copy(s)}
                title={`Copy path — ${s}`}>
                {copied === s ? '✓ copied' : s.split('/').pop()}
              </button>
            ))}
          </div>
        )}
      </div>
      {children}
      <div className="uth-foot">
        <b>To change this:</b>{' '}
        {editNote || (
          <>edit {sources.length ? <span className="mono">{sources.join(', ')}</span> : 'the source files'}{' '}
            and redeploy — the pipeline agents read the same files, so the change applies
            everywhere at once.</>
        )}
      </div>
    </>
  )
}

export function SectionCard({ id, title, sub, open, onToggle, innerRef, children }) {
  return (
    <div className="panel" ref={innerRef} id={`section-${id}`} style={{ marginTop: 14 }}>
      <div className="row between" style={{ cursor: 'pointer' }} onClick={() => onToggle(id)}>
        <button className="linklike" style={{ fontSize: 15, fontWeight: 600 }}>
          {open ? '▾' : '▸'} {title}
        </button>
        {sub && <span className="muted" style={{ fontSize: 12 }}>{sub}</span>}
      </div>
      {open && <div style={{ marginTop: 10 }}>{children}</div>}
    </div>
  )
}

export function PipelineSection({ data }) {
  if (!data) return <Unavailable what="The pipeline stages (sdr-pipeline SKILL.md)" />
  return (
    <>
      {data.stages.map((s) => (
        <div key={s.n} className="kv" style={{ marginBottom: 8 }}>
          <div className="muted">Stage {s.n}</div>
          <div><b>{s.name}</b><div className="muted" style={{ fontSize: 12 }}>{s.detail}</div></div>
        </div>
      ))}
      <div className="section-h">Persona → agent routing</div>
      <ChipRow>
        {Object.entries(data.agent_by_persona || {}).map(([p, agent]) => (
          <span key={p} style={{ marginRight: 14, display: 'inline-block', marginBottom: 6 }}>
            <Badge kind="persona" value={p} /> <span className="mono muted" style={{ fontSize: 12 }}>→ {agent}</span>
          </span>
        ))}
      </ChipRow>
    </>
  )
}

export function IcpFilterSection({ data }) {
  if (!data) return <Unavailable what="The ICP filter rules (buyer_group.py)" />
  return (
    <>
      <p className="muted" style={{ fontSize: 13 }}>{data.definition}</p>
      <table className="dense">
        <thead><tr><th></th><th>Role match</th><th>ICP</th><th>Persona</th><th>Titles matched</th></tr></thead>
        <tbody>
          {data.roles.map((r) => (
            <tr key={r.order}>
              <td className="muted">{r.order}</td>
              <td>
                <b>{r.role}</b>
                {r.note && <div className="muted" style={{ fontSize: 11 }}>{r.note}</div>}
              </td>
              <td>{r.icp
                ? <span style={{ color: BRAND.jade }}>✓ ICP</span>
                : <span style={{ color: BRAND.red }}>✗ not ICP</span>}
              </td>
              <td>{r.persona ? <Badge kind="persona" value={r.persona} /> : <span className="muted">skipped</span>}</td>
              <td>{(r.patterns.humanized || []).map((h) => <Chip key={h} label={h} />)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
        Matched top to bottom, first match wins. {data.fallthrough}.
      </p>
      <RawPatterns rows={data.roles.map((r) => ({ label: r.role, raw: r.patterns.raw }))} />
    </>
  )
}

export function PersonaAgentsSection({ data }) {
  if (!data || !data.length) return <Unavailable what="The persona agent definitions (.claude/agents)" />
  return (
    <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))' }}>
      {data.map((p) => (
        <div key={p.id} className="touch" style={{ borderLeft: `3px solid ${PERSONA_COLORS[p.id] || 'var(--border)'}` }}>
          <div className="row between">
            <b>{p.name}</b>
            <span className="mono muted" style={{ fontSize: 11 }}>{p.agent}</span>
          </div>
          <div className="kv" style={{ marginTop: 8 }}><div className="muted">Pain</div><div style={{ fontSize: 13 }}>{p.pain}</div></div>
          <div className="kv"><div className="muted">Outcome</div><div style={{ fontSize: 13 }}>{p.outcome}</div></div>
          <div className="kv"><div className="muted">Gives / CTAs</div><div style={{ fontSize: 13 }}>{p.ctas}</div></div>
          <div className="kv"><div className="muted">Tone</div><div style={{ fontSize: 13 }}>{p.tone}</div></div>
        </div>
      ))}
    </div>
  )
}

export function SequencingSection({ data }) {
  if (!data) return <Unavailable what="The sequencing structure (icp-email.md / cta-offers.md)" />
  const lib = data.cta_library || {}
  return (
    <>
      <div className="section-h" style={{ marginTop: 0 }}>The 4-touch email structure</div>
      <table className="dense">
        <thead><tr><th>Step</th><th>Job</th><th>CTA</th></tr></thead>
        <tbody>
          {(data.four_touch || []).map((r) => (
            <tr key={r.step}><td>{r.step}</td><td style={{ fontSize: 12 }}>{r.job}</td><td style={{ fontSize: 12 }}>{r.cta}</td></tr>
          ))}
        </tbody>
      </table>

      <div className="section-h">Plus 3 LinkedIn touches</div>
      {(data.linkedin_touches || []).map((t) => (
        <div key={t.key} className="kv"><div className="mono muted" style={{ fontSize: 12 }}>{t.key}</div><div style={{ fontSize: 13 }}>{t.desc}</div></div>
      ))}

      <div className="section-h">CTA / offer library (the give is delivered ON a meeting)</div>
      {['tier_a', 'tier_b'].map((tier) => (
        <div key={tier} style={{ marginBottom: 8 }}>
          <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
            {tier === 'tier_a' ? 'Tier A — product-as-the-give' : 'Tier B — analysis / teardown gives'}
          </div>
          {(lib[tier] || []).map((o) => (
            <div key={o.n} style={{ fontSize: 13, marginBottom: 6 }}>
              <b>{o.n}. {o.name}</b>{o.note && <span className="muted"> · {o.note}</span>}
              <div className="muted" style={{ fontSize: 12, fontStyle: 'italic' }}>"{o.cta}"</div>
            </div>
          ))}
        </div>
      ))}
      {(lib.anti_patterns || []).length > 0 && (
        <>
          <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>Never used</div>
          {lib.anti_patterns.map((a, i) => (
            <div key={i} style={{ fontSize: 12, color: BRAND.red }}>{a}</div>
          ))}
        </>
      )}

      {data.variants && (
        <>
          <div className="section-h">Instruction-set variants (A/B)</div>
          {data.variants.list.map((v) => (
            <div key={v.id} className="kv">
              <div>
                <span className="mono" style={{ fontSize: 12 }}>{v.id}</span>
                {v.id === data.variants.default && <div><span className="badge" style={{ color: BRAND.jade, borderColor: BRAND.jade }}>default</span></div>}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>{v.summary}</div>
            </div>
          ))}
        </>
      )}
    </>
  )
}

export function KnowledgeSection({ data }) {
  if (!data) return <Unavailable what="The knowledge base (offer.md)" />
  return (
    <>
      <p style={{ fontSize: 14 }}>{data.one_liner}</p>
      <div className="section-h">Proof the copy may cite (nothing else)</div>
      {(data.proof || []).map((p, i) => <div key={i} style={{ fontSize: 13, marginBottom: 4 }}>· {p}</div>)}
      <div className="section-h">Every writer is grounded in</div>
      <ChipRow>{(data.files || []).map((f) => <Chip key={f} label={f} />)}</ChipRow>
    </>
  )
}

export function GuardrailsSection({ data }) {
  if (!data) return <Unavailable what="The guardrails (icp-email.md / lint_sequence.py)" />
  return (
    <>
      <p className="muted" style={{ fontSize: 12 }}>
        {data.word_band}. {data.enforced_at}.
      </p>
      <div className="section-h" style={{ marginTop: 0 }}>Linter checks (every email, before enrollment)</div>
      <ChipRow>{(data.lint_checks || []).map((c) => <Chip key={c.id} label={c.label} />)}</ChipRow>
      <RawPatterns rows={(data.lint_checks || []).map((c) => ({ label: c.id, raw: c.raw }))} />
      <div className="section-h">Writing rules</div>
      {(data.rules || []).map((r, i) => <div key={i} style={{ fontSize: 12, marginBottom: 3 }}>· {r}</div>)}
    </>
  )
}

// Both signal engines get the same four-block layout — status, settings, scope,
// effect on copy — so they can be compared at a glance and a third engine has an
// obvious shape to follow.
function SignalStatus({ available, reason, cadence }) {
  return (
    <div className="row" style={{ gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 10 }}>
      <span className="badge" style={{
        color: available ? BRAND.jade : BRAND.amber,
        borderColor: available ? BRAND.jade : BRAND.amber,
      }}>
        {available ? '✓ operational' : '⚠ unavailable'}
      </span>
      <span className="muted" style={{ fontSize: 12 }}>{cadence}</span>
      {!available && reason && (
        <span style={{ fontSize: 12, color: BRAND.amber }}>· {reason}</span>
      )}
    </div>
  )
}

function SettingsTable({ rows }) {
  if (!rows?.length) return null
  return (
    <table className="dense" style={{ marginBottom: 4 }}>
      <thead><tr><th>Setting</th><th>Value</th><th>Effect</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key}>
            <td className="mono" style={{ fontSize: 11.5, whiteSpace: 'nowrap' }}>{r.key}</td>
            <td style={{ whiteSpace: 'nowrap' }}>
              <span className="mono" style={{
                fontSize: 11.5,
                color: r.secret
                  ? (r.value === 'set' ? BRAND.jade : BRAND.amber)
                  : (r.value === '0' ? BRAND.red : BRAND.ink),
              }}>{r.value}</span>
              {r.source === 'default' && <span className="muted" style={{ fontSize: 10.5 }}> (default)</span>}
            </td>
            <td className="muted" style={{ fontSize: 11.5 }}>{r.effect}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function SignalBlock({ name, purpose, data, unavailableWhat, cadence, children }) {
  return (
    <div className="touch" style={{ marginBottom: 14 }}>
      <div className="row between" style={{ alignItems: 'flex-start', gap: 12 }}>
        <div>
          <b style={{ fontSize: 14 }}>{name}</b>
          <div className="muted" style={{ fontSize: 12, marginTop: 2, maxWidth: 560 }}>{purpose}</div>
        </div>
      </div>
      {!data ? <div style={{ marginTop: 8 }}><Unavailable what={unavailableWhat} /></div> : (
        <div style={{ marginTop: 10 }}>
          <SignalStatus available={data.available} reason={data.reason} cadence={cadence(data)} />
          <div className="muted" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px', marginBottom: 6 }}>Settings</div>
          <SettingsTable rows={data.settings} />
          {children}
        </div>
      )}
    </div>
  )
}

export function SignalsSection({ data }) {
  const tech = data?.tech
  const hiring = data?.hiring
  return (
    <>
      <SignalBlock
        name="Technographics" data={tech}
        purpose="Which GTM tech an account runs. Deterministic DNS + website fingerprinting — no LLM, no third-party API."
        unavailableWhat="The technographic selection"
        cadence={(d) => `cached ${d.refresh_days} days`}
      >
        <div className="muted" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px', margin: '12px 0 6px' }}>
          Detection scope
        </div>
        {tech?.buckets.map((b) => (
          <div key={b.id} style={{ marginBottom: 6 }}>
            <div className="muted" style={{ fontSize: 12 }}>{b.name} ({b.vendors.length})</div>
            <ChipRow>
              {b.vendors.map((v) => (
                <Chip key={v.id} label={v.name} color={v.playbook ? PLAYBOOK_TONE[v.playbook] : undefined}
                  title={v.playbook ? PLAYBOOK_LABEL[v.playbook] : undefined} />
              ))}
            </ChipRow>
          </div>
        ))}
        <div className="muted" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px', margin: '12px 0 6px' }}>
          Effect on the copy
        </div>
        {tech?.playbooks.map((p) => (
          <div key={p.id} style={{ fontSize: 12, marginBottom: 3 }}>
            <span style={{ color: PLAYBOOK_TONE[p.id], fontWeight: 600 }}>{p.id.replace('_', '/')}</span>
            {' '}({p.vendors.join(', ')}) → {p.play}
          </div>
        ))}
      </SignalBlock>

      <SignalBlock
        name="Hiring" data={hiring}
        purpose="Is the account hiring, and specifically for sales/GTM roles? A non-empty sales subset is what email 2 opens on."
        unavailableWhat="The hiring-role taxonomy"
        cadence={(d) => `cached ${d.refresh_days} days · 1 credit per uncached scan`}
      >
        <div className="muted" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.5px', margin: '12px 0 6px' }}>
          Detection scope — counted as sales/GTM
        </div>
        {hiring?.include.map((g) => (
          <div key={g.group} style={{ marginBottom: 6 }}>
            <div className="muted" style={{ fontSize: 12 }}>{g.group} ({g.chips.length})</div>
            <ChipRow>{g.chips.map((c) => <Chip key={c.raw} label={c.label} />)}</ChipRow>
          </div>
        ))}
        <div className="muted" style={{ fontSize: 12 }}>
          Never counted{hiring?.exclude_beats_include ? ' (exclude beats include)' : ''}
        </div>
        <ChipRow>{hiring?.exclude.map((c) => <Chip key={c.raw} label={c.label} color={BRAND.red} />)}</ChipRow>
        <RawPatterns rows={[
          ...(hiring?.include || []).flatMap((g) => g.chips.map((c) => ({ label: 'include', raw: c.raw }))),
          ...(hiring?.exclude || []).map((c) => ({ label: 'exclude', raw: c.raw })),
        ]} />
      </SignalBlock>
    </>
  )
}
