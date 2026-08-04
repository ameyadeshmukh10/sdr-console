import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import CampaignBrief from './CampaignBrief.jsx'
import { ChannelPlan } from './campaignShared.jsx'

// Create a campaign: name it, set the target window, and define what "showing
// signal" means. The live match count under the window is the point of this
// dialog — the definition is abstract until you can see how many accounts it
// currently catches, and a window that catches nothing is the most common
// mistake here.
//
// Two ways in, same form: fill the fields, or describe the campaign at the top and
// let the configurator fill them (CampaignBrief). The second path only ever writes
// into this state — the fields it sets are visible and editable before Create — so
// there is no second, hidden definition of the campaign.

const PLAYBOOK = [
  { id: 'sequencing', label: 'Sequencing tools', hint: 'Outreach, Salesloft, Apollo' },
  { id: 'intent_abm', label: 'Intent / ABM', hint: '6sense, Demandbase, ZoomInfo' },
  { id: 'ads', label: 'Ad pixels', hint: 'they are spending on ads' },
]
const PERSONAS = ['sales-leadership', 'revops', 'partnerships', 'sdr-bdr']

function today(offsetDays = 0) {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  return d.toISOString().slice(0, 10)
}

export default function CampaignForm({ onClose, onCreated }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  // The agreed direction, in prose. No field of its own on the form — it is written
  // by the configurator and carried to the copy generator, so what was decided in
  // the room reaches the sequence instead of stopping at whoever filled this in.
  const [brief, setBrief] = useState('')
  const [briefed, setBriefed] = useState(null)   // which fields it set, for the badges
  const [windowStart, setWindowStart] = useState(today(-30))
  const [windowEnd, setWindowEnd] = useState(today(30))
  const [membershipMode, setMembershipMode] = useState('rolling')
  const [discoveryDays, setDiscoveryDays] = useState('7')
  const [evergreen, setEvergreen] = useState(false)
  const [evergreenDays, setEvergreenDays] = useState('30')
  const [kinds, setKinds] = useState(['research', 'hiring'])
  const [requireRecent, setRequireRecent] = useState(true)
  const [hiringSalesMin, setHiringSalesMin] = useState('')
  const [techPlaybook, setTechPlaybook] = useState([])
  const [personas, setPersonas] = useState([])
  const [motion, setMotion] = useState('outbound')
  const [audience, setAudience] = useState(null)   // set by the configurator only
  // Outbound or inbound. Not the same as the motion FILTER below: this is what
  // kind of campaign it is, and it changes how every touch is written.
  const [campaignType, setCampaignType] = useState('outbound')
  // The fit gate: signal says the ACCOUNT is worth working, this says whether
  // this person at it is.
  const [minScore, setMinScore] = useState('')
  const [requireSenior, setRequireSenior] = useState(false)
  // How this campaign reaches people. Email + LinkedIn by default; ads is a
  // declared plan (the advertising agent is roadmap).
  const [channels, setChannels] = useState({ email: true, linkedin: true, ads: false })
  const [variant, setVariant] = useState('value-give')
  const [bisonId, setBisonId] = useState('')
  const [heyreachId, setHeyreachId] = useState('')
  const [targetAccounts, setTargetAccounts] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [events, setEvents] = useState(null)
  const [vocab, setVocab] = useState(null)

  // Preview what the window would catch, from the signal event log. Advisory
  // only: it counts observations, not contacts, and the persona/motion filters
  // are applied server-side at qualification.
  useEffect(() => { api.signalEvents(365).then(setEvents).catch(() => {}) }, [])
  useEffect(() => { api.audienceVocab().then(setVocab).catch(() => {}) }, [])

  // The signal vocabulary comes from campaigns.SIGNAL_REGISTRY, so a kind added
  // there appears in this builder with no change to this file.
  const KINDS = (vocab?.signal_kinds || []).map((k) =>
    ({ id: k.id, label: k.label, hint: k.description }))

  const inWindow = (events?.events || []).filter((e) => {
    const at = (e.observed_at || '').slice(0, 10)
    if (windowStart && at < windowStart) return false
    if (windowEnd && at > windowEnd) return false
    return kinds.includes(e.kind)
  })
  const accountsInWindow = new Set(inWindow.map((e) => e.domain)).size

  function toggle(list, setList, id) {
    setList(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])
  }

  // Apply a configurator patch. Only keys PRESENT in the patch are touched, so an
  // answer to a follow-up question refines the configuration instead of resetting
  // everything the previous round decided (or anything typed by hand since).
  function applyConfig(cfg) {
    const set = (k, fn) => { if (cfg[k] !== undefined && cfg[k] !== null) fn(cfg[k]) }
    set('name', setName)
    set('description', setDescription)
    set('brief', setBrief)
    set('window_start', setWindowStart)
    set('window_end', setWindowEnd)
    set('membership_mode', setMembershipMode)
    set('variant', setVariant)
    set('audience', setAudience)
    set('discovery_interval_days', (v) => setDiscoveryDays(String(v)))
    if (cfg.evergreen !== undefined) setEvergreen(!!cfg.evergreen)
    set('campaign_type', setCampaignType)
    set('evergreen_interval_days', (v) => setEvergreenDays(String(v)))
    set('target_accounts', (v) => setTargetAccounts(String(v)))
    const q = cfg.signal_query || {}
    if (q.kinds) setKinds(q.kinds)
    if (q.require_recent !== undefined) setRequireRecent(!!q.require_recent)
    if (q.hiring_sales_min !== undefined && q.hiring_sales_min !== null) {
      setHiringSalesMin(String(q.hiring_sales_min))
    }
    if (q.tech_playbook) setTechPlaybook(q.tech_playbook)
    if (q.personas) setPersonas(q.personas)
    if (q.motion) setMotion(q.motion)
    if (q.min_score !== undefined && q.min_score !== null) setMinScore(String(q.min_score))
    if (q.require_senior !== undefined) setRequireSenior(!!q.require_senior)
    setBriefed((prev) => {
      const keys = new Set(prev || [])
      Object.keys(cfg).forEach((k) => keys.add(k))
      Object.keys(q).forEach((k) => keys.add(`signal_query.${k}`))
      return [...keys]
    })
  }

  const fromBrief = (k) => (briefed || []).includes(k)
  // The form state as the configurator sees it, so a follow-up round refines what
  // is on screen rather than re-deriving it from the prompt alone.
  const currentState = {
    name, description, window_start: windowStart, window_end: windowEnd,
    membership_mode: membershipMode, variant, audience,
    signal_query: { kinds, require_recent: requireRecent, motion, personas,
      tech_playbook: techPlaybook },
  }

  async function submit() {
    if (!name.trim()) { setError('Give the campaign a name.'); return }
    if (kinds.length === 0) { setError('Pick at least one signal type.'); return }
    setBusy(true); setError(null)
    const signal_query = {
      kinds, require_recent: requireRecent, motion,
      tech_playbook: techPlaybook, personas, exclude_enrolled: true,
    }
    if (hiringSalesMin !== '') signal_query.hiring_sales_min = Number(hiringSalesMin)
    if (minScore !== '') signal_query.min_score = Number(minScore)
    if (requireSenior) signal_query.require_senior = true
    try {
      const d = await api.createCampaign({
        name: name.trim(), description: description.trim() || undefined,
        brief: brief.trim() || undefined,
        audience: audience || undefined,
        channels,
        campaign_type: campaignType,
        window_start: windowStart || undefined, window_end: windowEnd || undefined,
        membership_mode: membershipMode, signal_query, variant,
        discovery_interval_days: Number(discoveryDays),
        evergreen,
        evergreen_interval_days: Number(evergreenDays),
        bison_campaign_id: bisonId || undefined,
        heyreach_campaign_id: heyreachId || undefined,
        target_accounts: targetAccounts || undefined,
      })
      onCreated(d.campaign.campaign_id)
    } catch (e) {
      setError(e.message); setBusy(false)
    }
  }

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer" style={{ width: 620, overflowY: 'auto' }}>
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
          <h2 style={{ margin: 0 }}>New campaign</h2>
          <button className="ghost sm" onClick={onClose}>Close</button>
        </div>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Define which accounts belong and over what period. The sequence and its per-step
          offers are set up next, pre-filled with the standard 4-touch cadence.
        </p>

        <ErrorBanner error={error} />

        <CampaignBrief onApply={applyConfig} current={currentState} />

        <Section title="What kind of campaign" set={fromBrief('campaign_type')}
          hint="Inbound isn't a filter — it changes how every touch is written.">
          <div className="opt-grid">
            <button type="button" onClick={() => { setCampaignType('outbound'); setMotion('outbound') }}
              className={'opt' + (campaignType === 'outbound' ? ' on' : '')}>
              <span className="opt-t">Outbound</span>
              <span className="opt-d">Cold. We found them on a signal and opened the conversation.</span>
            </button>
            <button type="button" onClick={() => { setCampaignType('inbound'); setMotion('inbound') }}
              className={'opt' + (campaignType === 'inbound' ? ' on' : '')}>
              <span className="opt-t">Inbound</span>
              <span className="opt-d">They came to us — a form, an event, a download, an
                identified visit. Copy never cold-opens, and the pipeline is reported
                as influenced rather than created.</span>
            </button>
          </div>
        </Section>

        <Field label="Name" set={fromBrief('name')}>
          <input value={name} onChange={(e) => setName(e.target.value)}
            placeholder="e.g. August funding + hiring push" style={{ width: '100%' }} />
        </Field>
        <Field label="What defines it" set={fromBrief('description')}
          hint="Optional. Shown on the campaign and given to the copy suggester as context.">
          <input value={description} onChange={(e) => setDescription(e.target.value)}
            placeholder="Accounts that raised or opened sales roles in August" style={{ width: '100%' }} />
        </Field>

        {/* The agreed direction. Editable here because it is prose the copy writer
            reads verbatim, and nobody should have to re-run the configurator to fix
            a sentence in it. */}
        {brief && (
          <Field label="The angle we agreed" set={fromBrief('brief')}
            hint="Given to the copy generator as direction for every touch in this campaign.">
            <textarea rows={4} value={brief} onChange={(e) => setBrief(e.target.value)}
              style={{ width: '100%', resize: 'vertical' }} />
          </Field>
        )}
        {audience && (
          <Field label="Audience" set={fromBrief('audience')}
            hint="Who is in the pool at all. Change it on the campaign's Audience tab after creating.">
            <div className="badge">{audienceLabel(audience)}</div>
          </Field>
        )}

        <Section title="Target window" set={fromBrief('window_start') || fromBrief('window_end')}
          hint="The period a signal must have fired in for an account to qualify.">
          <div className="row" style={{ gap: 10 }}>
            <label className="muted" style={{ fontSize: 12 }}>From{' '}
              <input type="date" value={windowStart} onChange={(e) => setWindowStart(e.target.value)} />
            </label>
            <label className="muted" style={{ fontSize: 12 }}>To{' '}
              <input type="date" value={windowEnd} onChange={(e) => setWindowEnd(e.target.value)} />
            </label>
          </div>
          <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            {events == null ? 'Checking what this window catches…'
              : events.available === false ? 'No signal history in this dataset yet.'
                : <><b style={{ color: accountsInWindow ? 'var(--green)' : 'var(--amber)' }}>
                  {num(accountsInWindow)} accounts</b> showed a matching signal in this window
                  {' '}({num(inWindow.length)} observations). Persona and motion filters are applied
                  on top when the campaign qualifies.</>}
          </div>
        </Section>

        <Section title="Membership" set={fromBrief('membership_mode') || fromBrief('discovery_interval_days')}>
          <Radio name="mm" value="rolling" cur={membershipMode} set={setMembershipMode}
            label="Rolling" hint="Accounts that fire during the window are added automatically, hourly." />
          <Radio name="mm" value="snapshot" cur={membershipMode} set={setMembershipMode}
            label="Snapshot" hint="Qualify once, then freeze the list." />
          {/* Evergreen. Not "runs forever" — runs in cycles and asks between them,
              because what goes stale in an always-on campaign is the message, not
              the targeting. */}
          <label className="row" style={{ gap: 8, alignItems: 'flex-start', marginTop: 10 }}>
            <input type="checkbox" checked={evergreen} style={{ marginTop: 2, width: 'auto' }}
              onChange={(e) => setEvergreen(e.target.checked)} />
            <span>
              <span style={{ fontSize: 13 }}>Keep this running (evergreen)</span>
              <span className="muted" style={{ fontSize: 11, display: 'block' }}>
                At the end of each cycle it pauses and asks you to confirm or change
                the messaging, then opens a fresh window. It never relaunches itself
                unattended.
              </span>
            </span>
          </label>
          {evergreen && (
            <label className="muted" style={{ fontSize: 12, display: 'block',
              marginTop: 6, marginLeft: 22 }}>
              Come back to me every{' '}
              <select value={evergreenDays} onChange={(e) => setEvergreenDays(e.target.value)}>
                <option value="14">14 days</option>
                <option value="30">30 days</option>
                <option value="60">60 days</option>
                <option value="90">90 days</option>
              </select>
            </label>
          )}
          <label className="muted" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
            Re-scan for new accounts every{' '}
            <select value={discoveryDays} onChange={(e) => setDiscoveryDays(e.target.value)}>
              <option value="7">7 days</option>
              <option value="14">14 days</option>
              <option value="30">30 days</option>
              <option value="0">never (manual only)</option>
            </select>
          </label>
          <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
            Scanning finds accounts that have no signal on file yet. Hiring lookups cost one
            Prospeo credit per account, so background scans are capped per run.
          </div>
        </Section>

        <Section title="Showing signal means" set={fromBrief('signal_query.kinds')} hint="At least one of these must have fired in the window.">
          {KINDS.map((k) => (
            <Check key={k.id} checked={kinds.includes(k.id)} onChange={() => toggle(kinds, setKinds, k.id)}
              label={k.label} hint={k.hint} />
          ))}
          {kinds.includes('research') && (
            <div style={{ marginLeft: 22 }}>
              <Check checked={requireRecent} onChange={() => setRequireRecent(!requireRecent)}
                label="Real dated event only"
                hint="Exclude the product/GTM fallback anchor used when nothing recent was found." />
            </div>
          )}
          {kinds.includes('hiring') && (
            <div style={{ marginLeft: 22 }}>
              <label className="muted" style={{ fontSize: 12 }}>
                Minimum open sales roles{' '}
                <input type="number" min="0" style={{ width: 70 }} value={hiringSalesMin}
                  placeholder="any" onChange={(e) => setHiringSalesMin(e.target.value)} />
              </label>
            </div>
          )}
          {kinds.includes('tech') && (
            <div style={{ marginLeft: 22 }}>
              <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
                Require one of these plays (leave empty for any detected stack):
              </div>
              {PLAYBOOK.map((p) => (
                <Check key={p.id} checked={techPlaybook.includes(p.id)}
                  onChange={() => toggle(techPlaybook, setTechPlaybook, p.id)}
                  label={p.label} hint={p.hint} />
              ))}
            </div>
          )}
        </Section>

        <Section title="Who to contact at those accounts" set={fromBrief('signal_query.personas') || fromBrief('signal_query.motion')}>
          <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
            {PERSONAS.map((p) => (
              <button key={p} type="button"
                className={personas.includes(p) ? 'primary sm' : 'ghost sm'}
                onClick={() => toggle(personas, setPersonas, p)}>{p}</button>
            ))}
          </div>
          <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
            {personas.length === 0 ? 'All personas.' : `${personas.length} selected.`}
          </div>
          <label className="muted" style={{ fontSize: 12 }}>
            Motion{' '}
            <select value={motion} onChange={(e) => setMotion(e.target.value)}>
              <option value="outbound">outbound only</option>
              <option value="inbound">inbound only</option>
              <option value="any">any</option>
            </select>
          </label>
          <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            Outbound-only keeps inbound-sourced contacts out, so the campaign's numbers stay
            attributable to cold outbound.
          </div>

          {/* Signal says the ACCOUNT is worth working. This says whether the person
              at it is — without it, a qualifying account sweeps in everyone we hold
              there and the campaign quietly becomes a blast. */}
          <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px dashed var(--border)' }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
              Only add them if they're also a fit
            </div>
            <div className="row" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
              <label className="muted" style={{ fontSize: 12 }}>
                Minimum score{' '}
                <select value={minScore} onChange={(e) => setMinScore(e.target.value)}>
                  <option value="">no minimum</option>
                  <option value="45">45 — warm and above</option>
                  <option value="70">70 — hot only</option>
                  <option value="30">30 — anything but the weakest</option>
                </select>
              </label>
              <label className="row" style={{ gap: 6, alignItems: 'center' }}>
                <input type="checkbox" checked={requireSenior} style={{ width: 'auto' }}
                  onChange={(e) => setRequireSenior(e.target.checked)} />
                <span style={{ fontSize: 12 }}>Senior buyers only</span>
              </label>
            </div>
            <div className="muted" style={{ fontSize: 11, marginTop: 5 }}>
              The score blends signal strength, how many signal families stacked, and
              persona fit — so it already IS the fit measure. Contacts below the bar
              stay out and are counted as <span className="mono">below_fit</span>.
            </div>
          </div>
        </Section>

        <Section title="How it reaches people"
          hint="The same accounts get worked differently depending on the play — direct
                touches for the people worth one, ads across the account to build
                familiarity underneath them.">
          <ChannelPlan value={channels} onChange={setChannels} />
        </Section>

        <Section title="Sending" set={fromBrief('variant') || fromBrief('target_accounts')} hint="This campaign owns one Bison email campaign and one HeyReach campaign.">
          <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
            <label className="muted" style={{ fontSize: 12 }}>Bison campaign id{' '}
              <input style={{ width: 90 }} value={bisonId} onChange={(e) => setBisonId(e.target.value)} placeholder="14" />
            </label>
            <label className="muted" style={{ fontSize: 12 }}>HeyReach id{' '}
              <input style={{ width: 110 }} value={heyreachId} onChange={(e) => setHeyreachId(e.target.value)} />
            </label>
            <label className="muted" style={{ fontSize: 12 }}>Variant{' '}
              <select value={variant} onChange={(e) => setVariant(e.target.value)}>
                <option value="value-give">value-give</option>
                <option value="earn">earn</option>
                <option value="show">show</option>
              </select>
            </label>
            <label className="muted" style={{ fontSize: 12 }}>Cap accounts{' '}
              <input type="number" min="1" style={{ width: 80 }} value={targetAccounts}
                placeholder="none" onChange={(e) => setTargetAccounts(e.target.value)} />
            </label>
          </div>
        </Section>

        <div className="row" style={{ gap: 10, marginTop: 18 }}>
          <button className="primary" disabled={busy} onClick={submit}>
            {busy ? <Spinner /> : 'Create campaign'}
          </button>
          <button className="ghost" onClick={onClose}>Cancel</button>
        </div>
        <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          Created as a draft. Nothing is qualified or enrolled until you say so.
        </div>
      </div>
    </>
  )
}

function audienceLabel(a) {
  if (!a) return ''
  if (a.type === 'hubspot_list') return `HubSpot list ${a.list_name || a.list_id}`
  if (a.type === 'crm_query') {
    return `${a.preset.replace(/_/g, ' ')}${a.days ? ` · last ${a.days} days` : ''}`
  }
  return 'Everyone in the pipeline'
}

// `set` marks a field the configurator filled in. A small mark, not a lock: the
// value is ordinary form state and typing over it is expected — the mark only says
// where it came from, so nothing on the screen has unexplained content.
function Field({ label, hint, set, children }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div className="row" style={{ gap: 6, alignItems: 'center', marginBottom: 4 }}>
        <span style={{ fontSize: 12, fontWeight: 600 }}>{label}</span>
        {set && <span className="from-brief" title="Set from your description">✦</span>}
      </div>
      {children}
      {hint && <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>{hint}</div>}
    </div>
  )
}

function Section({ title, hint, set, children }) {
  return (
    <div style={{ marginBottom: 18, paddingTop: 14, borderTop: '1px solid var(--border)' }}>
      <div className="row" style={{ gap: 6, alignItems: 'center',
        marginBottom: hint ? 2 : 8 }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>{title}</span>
        {set && <span className="from-brief" title="Set from your description">✦</span>}
      </div>
      {hint && <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}>{hint}</div>}
      {children}
    </div>
  )
}

function Check({ checked, onChange, label, hint }) {
  return (
    <label className="row" style={{ gap: 8, alignItems: 'flex-start', marginBottom: 6, cursor: 'pointer' }}>
      <input type="checkbox" checked={checked} onChange={onChange} style={{ marginTop: 2, width: 'auto' }} />
      <span>
        <span style={{ fontSize: 13 }}>{label}</span>
        {hint && <span className="muted" style={{ fontSize: 11, display: 'block' }}>{hint}</span>}
      </span>
    </label>
  )
}

function Radio({ name, value, cur, set, label, hint }) {
  return (
    <label className="row" style={{ gap: 8, alignItems: 'flex-start', marginBottom: 6, cursor: 'pointer' }}>
      <input type="radio" name={name} checked={cur === value} onChange={() => set(value)}
        style={{ marginTop: 2, width: 'auto' }} />
      <span>
        <span style={{ fontSize: 13 }}>{label}</span>
        {hint && <span className="muted" style={{ fontSize: 11, display: 'block' }}>{hint}</span>}
      </span>
    </label>
  )
}
