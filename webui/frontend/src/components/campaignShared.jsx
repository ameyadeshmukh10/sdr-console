import { useEffect, useRef, useState } from 'react'

// Presentational helpers shared by every campaign surface (the Use page's
// campaign tab, the detail view, the Home widget, Analytics).
//
// These live here rather than on the page that first used them because
// CampaignDetail needs them too, and importing them back out of the page module
// created a real import cycle (page -> detail -> page). Rollup hoisted through it
// in a production build, so it only surfaced under the Vite dev server.
//
// Everything here styles through classes in styles.css — same radii, hues and
// rhythm as the rest of the console.

export function StatusBadge({ status }) {
  const cls = { active: 'status-enrolled', paused: 'status-skipped',
    draft: 'status-pending', completed: 'status-pending',
    archived: 'status-pending' }[status] || 'status-pending'
  return <span className={`badge ${cls}`}>{status || 'draft'}</span>
}

// The window as a human phrase. On an active rolling campaign the days-left is the
// fact that matters — it is when membership stops growing.
export function windowLabel(c) {
  const s = (c.window_start || '').slice(0, 10)
  const e = (c.window_end || '').slice(0, 10)
  if (!s && !e) return 'open-ended'
  if (s && !e) return `from ${s}`
  if (!s && e) return `until ${e}`
  return `${s} → ${e}`
}

export const BAND_COLOR = { hot: 'var(--red)', warm: 'var(--amber)', cool: 'var(--muted)' }

// Priority = signal strength at qualification. Frozen there, so the ordering of a
// list stays stable while it is being worked.
export function ScoreBadge({ score, band, detail }) {
  if (score == null) return <span className="muted">—</span>
  const parts = Object.entries(detail?.components || {})
    .map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`)
  const why = parts.length ? `${band} — ${parts.join(', ')}` : band
  return <span className={`score ${band || 'cool'}`} title={why}>{Math.round(score)}</span>
}

// The money scale — the aggregate signal as $ to $$$$$.
//
// Two axes, on purpose, because a rep weighs two different things before picking
// up the phone and one number cannot carry both:
//
//   how many $   how much is here      (score: signal strength + ICP fit)
//   what colour  how ready they are    (heat: warming? inbound?)
//
// So a perfect-fit account nobody has warmed reads $$$$$ in the cool "open" hue —
// lots of opportunity, no evidence they are ready — while an inbound contact reads
// $ in hot: small, but they came to us. The count and the colour disagreeing is the
// interesting case, not a bug.
//
// Understated on purpose: no legend, no header beyond the glyph column itself. It
// rewards noticing, and the tooltip explains it to anyone who hovers.
const MONEY_WHY = {
  hot: 'inbound — they came to us',
  warming: 'signal strengthening since last scored',
  open: 'strong on fit, no sign yet that they are ready',
  cold: 'little on file',
}

const COMPONENT_LABEL = {
  signal_strength: 'signal', stacking: 'stacking', persona_fit: 'persona fit',
}

export function Money({ value, detail }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const away = (e) => { if (!ref.current?.contains(e.target)) setOpen(false) }
    const esc = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', away)
    document.addEventListener('keydown', esc)
    return () => {
      document.removeEventListener('mousedown', away)
      document.removeEventListener('keydown', esc)
    }
  }, [open])

  if (!value || !value.level) return <span className="muted">—</span>
  const { level, heat, score, momentum, base } = value
  const why = `${'$'.repeat(level)} of $$$$$ — aggregate signal ${Math.round(score || 0)}`
    + `\n${MONEY_WHY[heat] || heat}\nClick to see how it was earned`
  const components = Object.entries(detail?.components || {})

  return (
    <span className="money-wrap" ref={ref}>
      <button type="button" className={`money heat-${heat || 'cold'}` + (open ? ' ringing' : '')}
        title={why} aria-label={why} aria-expanded={open}
        onClick={() => setOpen((v) => !v)}>
        {[1, 2, 3, 4, 5].map((i) => (
          <span key={i} className={i <= level ? 'on' : 'off'}
            style={{ '--i': i }}>$</span>
        ))}
      </button>

      {/* The receipt. The glyph says how much and how warm; this says how it was
          earned, itemised — which is the question anyone who bothers to click is
          asking. Printed on the spot, one line at a time, because a score that
          shows its working should look like it is showing its working. */}
      {open && (
        <div className="receipt" role="dialog">
          <div className="receipt-h">AGGREGATE SIGNAL</div>
          {components.length === 0 && (
            <div className="receipt-line"><span>no breakdown recorded</span><span /></div>
          )}
          {components.map(([k, v], i) => (
            <div className="receipt-line" key={k} style={{ '--i': i }}>
              <span>{COMPONENT_LABEL[k] || k.replace(/_/g, ' ')}</span>
              <b>{Number(v).toFixed(1)}</b>
            </div>
          ))}
          <div className="receipt-rule" />
          {/* TOTAL is the SUM of the lines above it — the priority score. The rank
              below is that plus the momentum adjustment, and it is what the glyph
              count is cut from. Printing the rank as the total made a receipt whose
              own lines did not add up. */}
          <div className="receipt-line total" style={{ '--i': components.length }}>
            <span>TOTAL</span><b>{Number(base ?? score ?? 0).toFixed(1)}</b>
          </div>
          {momentum != null && momentum !== 0 && (
            <>
              <div className="receipt-line" style={{ '--i': components.length + 1 }}>
                <span>momentum</span>
                <b className={momentum > 0 ? 'up' : 'down'}>
                  {momentum > 0 ? '+' : ''}{Number(momentum).toFixed(1)}
                </b>
              </div>
              <div className="receipt-line total" style={{ '--i': components.length + 2 }}>
                <span>RANKED AT</span><b>{Number(score || 0).toFixed(1)}</b>
              </div>
            </>
          )}
          <div className="receipt-foot" style={{ '--i': components.length + 3 }}>
            {MONEY_WHY[heat] || heat}
          </div>
          {/* Where a receipt puts its thank-you line. */}
          <div className="receipt-tag" style={{ '--i': components.length + 4 }}>
            marketing so easy sales can do it
          </div>
        </div>
      )}
    </span>
  )
}

// Movement since this contact was last scored. Direction is the news: an account
// warming up is a better call than a statically-equal one going cold.
export function Momentum({ value }) {
  if (value == null || value === 0) return <span className="muted">—</span>
  const up = value > 0
  return (
    <span className={'trend ' + (up ? 'up' : 'down')}
      title={up ? 'Signal strengthening since last scored'
        : 'Signal weakening since last scored'}>
      <svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor"
        strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        {up ? <polyline points="2 8 6 4 10 8" /> : <polyline points="2 4 6 8 10 4" />}
      </svg>
      {Math.abs(Math.round(value))}
    </span>
  )
}

// Which channels this contact is worth spending on. Sending capacity is finite, so
// the score has to say WHERE to spend, not only who is best.
const ICO = {
  width: 13, height: 13, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 1.9, strokeLinecap: 'round', strokeLinejoin: 'round',
}
const CHANNEL = {
  call: { label: 'Call', icon: <svg {...ICO}><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.2a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z" /></svg> },
  linkedin: { label: 'LinkedIn', icon: <svg {...ICO}><rect x="3" y="3" width="18" height="18" rx="3" /><path d="M7 10v7M7 7v.01M11 17v-4a2 2 0 0 1 4 0v4" /></svg> },
  email: { label: 'Email', icon: <svg {...ICO}><rect x="2.5" y="5" width="19" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></svg> },
  ads: { label: 'Ads', icon: <svg {...ICO}><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="4" /></svg> },
}
const ORDER = ['call', 'linkedin', 'email', 'ads']

export function Channels({ value }) {
  const on = value?.channels || {}
  const why = value?.why || {}
  if (!Object.keys(on).length) return <span className="muted">—</span>
  return (
    <span className="chan">
      {ORDER.map((k) => (
        <i key={k} className={on[k] ? 'on' : undefined}
          title={`${CHANNEL[k].label} — ${on[k] ? 'recommended' : 'not recommended'}. ${why[k] || ''}`}>
          {CHANNEL[k].icon}
        </i>
      ))}
    </span>
  )
}

// Every campaign this person is in. Membership lives on the PERSON: a contact
// worked by three campaigns has to read as three wherever they appear, or nobody
// notices they are being hit from three directions.
export function CampaignTags({ campaigns, current, compact = false }) {
  const list = campaigns || []
  if (!list.length) return <span className="muted">—</span>
  // COMPACT: one line, whatever the count. Full-width name pills are right on a
  // campaign's own screens, where there are one or two; on a dense contact-centric
  // table they wrap to three lines and make every row a different height. One
  // campaign shows its name, several show the count, and the names are one hover
  // away either way.
  if (compact) {
    const title = list.map((c) => `${c.name} — ${c.state}`).join('\n')
      + (list.length > 1
        ? '\n\nTouches across these are merged into one spaced cadence, so they are '
          + 'never contacted twice in a day.'
        : '')
    return (
      <span className={'tag one-line' + (list.length > 1 ? ' merged' : '')} title={title}>
        {list.length === 1 ? list[0].name : `${list.length} campaigns`}
      </span>
    )
  }
  return (
    <span className="tags">
      {list.map((c) => (
        <span key={c.campaign_id}
          className={'tag' + (c.campaign_id === current ? ' cur' : '')}
          title={`${c.name} — ${c.state}${c.score != null ? `, score ${Math.round(c.score)}` : ''}`}>
          {c.name}
        </span>
      ))}
      {list.length > 1 && (
        <span className="tag merged"
          title="In more than one campaign — their touches are merged into one spaced cadence so they are never contacted twice in a day.">
          merged
        </span>
      )}
    </span>
  )
}

// Hot / warm / cool split as one stacked bar — the shape of a call list at a
// glance, which says more here than any single number.
export function BandMix({ bands, avg }) {
  const b = bands || {}
  const total = (b.hot || 0) + (b.warm || 0) + (b.cool || 0)
  if (!total) return <span className="muted">not scored</span>
  const parts = [['hot', b.hot], ['warm', b.warm], ['cool', b.cool]]
  return (
    <div title={`${b.hot || 0} hot · ${b.warm || 0} warm · ${b.cool || 0} cool`
      + (avg != null ? ` · average ${avg}` : '')}>
      <div className="mix">
        {parts.map(([k, n]) => (n ? (
          <span key={k} className={k} style={{ width: `${(100 * n) / total}%` }} />
        ) : null))}
      </div>
      <div className="hint">
        {parts.filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(' · ')}
      </div>
    </div>
  )
}

// The numbered workflow rail. A campaign IS an ordered set of decisions — who,
// then find more of them, then what you say, then who to work first — so the
// navigation says so instead of being four undifferentiated tabs.
export function Steps({ steps, current, onSelect }) {
  const i = steps.findIndex((s) => s.id === current)
  return (
    <nav className="steps">
      {steps.map((s, n) => (
        <button key={s.id} type="button"
          className={'step' + (s.id === current ? ' active' : n < i ? ' done' : '')}
          onClick={() => onSelect(s.id)}>
          <span className="step-n">{n + 1}</span>{s.label}
        </button>
      ))}
    </nav>
  )
}

// Which channels this campaign runs on.
//
// The same account list is worked differently depending on the play, and that
// choice belongs on the campaign rather than being implied by which sender happens
// to be bound: LinkedIn only, for a senior committee you don't want to bulk-email.
// Email for volume. Ads across the whole account to build familiarity so the other
// two land warmer. Declaring it makes the plan legible and gives the per-contact
// channel recommendations a boundary to work inside.
//
// `ads` is ROADMAP and says so. Checking it records the intent and sizes the
// audience from the buying groups already mapped — it does not buy anything, and a
// checkbox that silently did nothing would be worse than no checkbox.
const CHANNELS = [
  { id: 'email', label: 'Email', hint: 'Sequenced through Email Bison, per contact.' },
  { id: 'linkedin', label: 'LinkedIn', hint: 'Connect + messages through HeyReach, per contact.' },
  { id: 'ads', label: 'Advertising', roadmap: true,
    hint: 'Account-level reach to the whole buying group — the familiarity layer '
        + 'under the direct touches. The advertising agent is on the roadmap; '
        + 'ticking this records the intent and sizes the audience.' },
]

export function ChannelPlan({ value, onChange, reach, disabled }) {
  const v = value || {}
  return (
    <div className="chan-plan">
      {CHANNELS.map((c) => (
        <label key={c.id} className={'chan-opt' + (v[c.id] ? ' on' : '')}>
          <input type="checkbox" checked={!!v[c.id]} disabled={disabled}
            onChange={(e) => onChange({ ...v, [c.id]: e.target.checked })} />
          <span className="chan-opt-body">
            <span className="chan-opt-t">
              {c.label}
              {c.roadmap && <span className="chan-roadmap">roadmap</span>}
            </span>
            <span className="chan-opt-d">{c.hint}</span>
            {c.id === 'ads' && v.ads && reach?.accounts > 0 && (
              <span className="chan-reach">
                Would reach {reach.accounts} accounts · {reach.contacts} mapped buyers
              </span>
            )}
          </span>
        </label>
      ))}
    </div>
  )
}

// Read-only summary for the campaign header.
export function ChannelChips({ value }) {
  const v = value || {}
  const on = CHANNELS.filter((c) => v[c.id])
  if (!on.length) return null
  return (
    <span className="row" style={{ gap: 4 }}>
      {on.map((c) => (
        <span key={c.id} className={'badge' + (c.roadmap ? ' chan-badge-roadmap' : '')}
          title={c.roadmap ? 'Declared — the advertising agent is roadmap' : c.hint}>
          {c.label}{c.roadmap ? ' (planned)' : ''}
        </span>
      ))}
    </span>
  )
}
