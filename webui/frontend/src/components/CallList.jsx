import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from './ui.jsx'
import { ScoreBadge, Momentum, Channels, CampaignTags, Money } from './campaignShared.jsx'
import ColumnPicker, { loadColumns, saveColumns } from './ColumnPicker.jsx'
import Addon from './Addon.jsx'
import ContactLink, { Phone } from './ContactLink.jsx'
import ContactActions, { EngagementBadge } from './ContactActions.jsx'
import { Link } from 'react-router-dom'
import {
  useSort, sortRows, SortTh, Search, Pick, Toggle, FilterCount, facet, matcher,
} from './tableTools.jsx'

// The SDR call list: every contact qualified into a campaign, strongest signal
// first, across all campaigns. A rep works one list, not one list per campaign.
//
// The score is frozen at qualification, so the order is stable while it is being
// worked — a list that silently reshuffled overnight would be unworkable. Ordering
// is by rank_score, which folds in momentum: an account warming up outranks a
// statically-equal one going cold.
//
// Columns are configurable and include anything mapped in the CRM field map, because
// the column a team actually needs is the one specific to how they work.
//
// FILTERING AND SORTING sit on top of that default rather than replacing it. The
// default order is the account-diverse interleave (every account's best contact
// before any account's second), which is a property of the query and cannot be
// reproduced by sorting a column — so "no column sort" stays a reachable state and
// is what the reset returns you to. Sorting by Score gives the raw ranking instead,
// which is the right answer to a different question and clusters big accounts at
// the top; the header says so.

// `state` is a SERVER filter — the response only ever contains the state asked for,
// so filtering it in the browser (as this did) meant picking "Enrolled" filtered a
// payload of nothing but qualified rows and always came back empty.
const STATES = [
  { id: 'qualified', label: 'Not yet worked' },
  { id: 'enrolled', label: 'Enrolled' },
  { id: 'removed', label: 'Not a fit' },
  { id: 'all', label: 'All states' },
]

const name = (m) => `${m.first_name || ''} ${m.last_name || ''}`.trim() || m.contact_id

// id -> { label, width, render, sort }. `sort` is the value the column orders on;
// omit it for a column with no meaningful order (icon rows). Keeping it beside the
// renderer is what stops the two drifting apart. `crm:<key>` ids fall through to
// the CRM renderer below.
const COLUMNS = {
  money: { label: '$', w: '6%', sort: (m) => m.money?.level ?? null, dir: 'desc',
    title: 'Sort by aggregate signal', render: (m) => <Money value={m.money} detail={m.score_detail} /> },
  score: { label: 'Score', w: '5%', sort: (m) => m.priority_score, dir: 'desc',
    title: 'Sort by raw score. The default order interleaves accounts so one company '
      + 'cannot fill the top of the list; this ranking does not.',
    render: (m) => <ScoreBadge score={m.priority_score} band={m.score_band} detail={m.score_detail} /> },
  momentum: { label: 'Trend', w: '5%', sort: (m) => m.momentum, dir: 'desc', render: (m) => <Momentum value={m.momentum} /> },
  name: { label: 'Name', w: '14%', sort: (m) => name(m), dir: 'asc', render: (m) => (
    <span className="row" style={{ gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
      <ContactLink contact={m} /><EngagementBadge member={m} />
    </span>) },
  phone: { label: 'Phone', w: '9%', sort: (m) => (m.phone || m.mobile_phone ? 1 : 0), dir: 'desc',
    title: 'Sort contacts with a number to the top', render: (m) => <Phone contact={m} /> },
  company: { label: 'Company', w: '11%', cls: 'muted', sort: (m) => m.company || m.domain, dir: 'asc', render: (m) => m.company || m.domain },
  title: { label: 'Title', w: '12%', cls: 'muted', sort: (m) => m.title, dir: 'asc', render: (m) => <span className="clamp2" title={m.title}>{m.title || '—'}</span> },
  persona: { label: 'Persona', w: '10%', sort: (m) => m.persona, dir: 'asc', render: (m) => (m.persona ? <span className={`badge persona-${m.persona}`}>{m.persona}</span> : '—') },
  buyer_role: { label: 'Buyer role', w: '11%', cls: 'muted', sort: (m) => m.buyer_role, dir: 'asc', render: (m) => m.buyer_role || '—' },
  channels: { label: 'Channels', w: '10%', render: (m) => <Channels value={m.channels} /> },
  campaigns: { label: 'Campaigns', w: '14%', sort: (m) => (m.all_campaigns || []).length, dir: 'desc',
    title: 'Sort by how many campaigns this person is in — the most-touched first',
    render: (m) => <CampaignTags campaigns={m.all_campaigns} current={m.campaign_id} /> },
  state: { label: 'State', w: '7%', sort: (m) => m.state, dir: 'asc', render: (m) => (
    <span className="row" style={{ gap: 4, alignItems: 'center', flexWrap: 'wrap' }}>
      <span className={`badge status-${m.state}`}>{m.state}</span>
      {m.outcome && <span className="badge" title="Last outcome">{m.outcome.replace(/_/g, ' ')}</span>}
      {m.snoozed_until && <span className="badge" title={`Snoozed until ${m.snoozed_until}`}>💤</span>}
    </span>) },
  signal: {
    label: 'Why they’re here', w: '18%', cls: 'muted',
    sort: (m) => m.signal_snapshot?.summary, dir: 'asc',
    render: (m) => <span className="clamp2" title={m.signal_snapshot?.summary}>
      {m.signal_snapshot?.summary || 'no recorded signal'}</span>,
  },
  signal_kind: { label: 'Signal type', w: '8%', sort: (m) => m.signal_kind, dir: 'asc', render: (m) => (m.signal_kind ? <span className="badge">{m.signal_kind.replace(/_/g, ' ')}</span> : '—') },
  origin: { label: 'Source', w: '8%', cls: 'muted', sort: (m) => m.origin, dir: 'asc', render: (m) => (m.origin === 'enriched' ? <span className="badge">enriched</span> : 'existing') },
  qualified_at: { label: 'Qualified', w: '8%', cls: 'muted', sort: (m) => m.qualified_at, dir: 'desc', render: (m) => (m.qualified_at || '').slice(0, 10) },
  email: { label: 'Email', w: '15%', cls: 'muted mono', sort: (m) => m.email, dir: 'asc', render: (m) => m.email || '—' },
}

// CRM-backed columns read the value the console computed for that field, which is
// exactly what it pushes to the CRM — so the column and the CRM agree by construction.
const CRM_VALUE = {
  priority_score: (m) => (m.priority_score == null ? '—' : Math.round(m.priority_score)),
  priority_band: (m) => m.score_band || '—',
  campaign_name: (m) => (m.all_campaigns || []).map((c) => c.name).join('; ') || '—',
  buyer_role: (m) => m.buyer_role || '—',
  recommended_channels: (m) => {
    const on = Object.entries((m.channels || {}).channels || {}).filter(([, v]) => v)
    return on.length ? on.map(([k]) => k).join(', ') : '—'
  },
}

function column(id) {
  if (COLUMNS[id]) return { id, ...COLUMNS[id] }
  if (id.startsWith('crm:')) {
    const key = id.slice(4)
    const render = CRM_VALUE[key] || (() => '—')
    return {
      id, label: key.replace(/_/g, ' '), w: '11%', cls: 'muted', render,
      // A wired CRM field becomes sortable for free, on the same value it renders —
      // which is the whole point of the field map being data.
      sort: CRM_VALUE[key] ? (m) => render(m) : undefined,
      dir: 'asc',
    }
  }
  return null
}

// The band a contact fell into at qualification. Filtered client-side because the
// counts above the table are drawn from the same loaded set — a server filter would
// make the tiles describe a different population than the list.
const BANDS = [
  { value: 'hot', label: 'hot' }, { value: 'warm', label: 'warm' }, { value: 'cool', label: 'cool' },
]

const EMPTY_FILTERS = {
  q: '', band: '', campaign: '', persona: '', kind: '',
  hideSnoozed: false, hideInactive: false,
}

const PAGE = 300

export default function CallList() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [state, setState] = useState('qualified')
  const [limit, setLimit] = useState(PAGE)
  const [f, setF] = useState(EMPTY_FILTERS)
  const [cols, setCols] = useState(loadColumns)
  const { sort, toggle, reset: resetSort } = useSort()

  function load() {
    setBusy(true)
    api.callList(null, limit, state)
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false))
  }
  // State and the row cap are server-side, so changing either refetches.
  useEffect(() => { load() }, [state, limit])

  function setColumns(ids) { setCols(ids); saveColumns(ids) }
  function set(k, v) { setF((prev) => ({ ...prev, [k]: v })) }

  const all = data?.contacts || []
  const total = data?.total ?? all.length
  const truncated = all.length < total

  // Facets come off the loaded set, so every option shown is one that can actually
  // return rows.
  const campaigns = facet(all, (m) => m.campaign_id, (v, m) => m.campaign_name || `#${v}`)
  const personas = facet(all, (m) => m.persona)
  const kinds = facet(all, (m) => m.signal_kind, (v) => v.replace(/_/g, ' '))

  const hit = matcher(f.q, (m) => [name(m), m.company, m.domain, m.title, m.email,
    m.buyer_role, m.signal_snapshot?.summary])
  const today = new Date().toISOString().slice(0, 10)

  const filtered = all.filter((m) => (
    (!f.band || m.score_band === f.band)
    && (!f.campaign || String(m.campaign_id) === f.campaign)
    && (!f.persona || m.persona === f.persona)
    && (!f.kind || m.signal_kind === f.kind)
    // A snoozed member is still a member — this hides them from the working view,
    // it does not remove them from the campaign.
    && (!f.hideSnoozed || !m.snoozed_until || m.snoozed_until <= today)
    && (!f.hideInactive || !m.engagement_state || m.engagement_state === 'active')
    && hit(m)
  ))
  const rows = sortRows(filtered, sort, Object.fromEntries(
    Object.entries(COLUMNS).map(([id, c]) => [id, c.sort]).filter(([, s]) => s)))

  const counts = all.reduce((a, m) => {
    if (m.score_band) a[m.score_band] = (a[m.score_band] || 0) + 1
    return a
  }, {})
  const active = cols.map(column).filter(Boolean)
  const dirty = Object.keys(EMPTY_FILTERS).some((k) => f[k] !== EMPTY_FILTERS[k])
  const stateLabel = STATES.find((s) => s.id === state)?.label
  const bandScope = truncated ? ` · of ${num(all.length)} loaded` : ''

  return (
    <div>
      <div className="row" style={{ gap: 8, marginBottom: 2 }}>
        <Addon id="lead-scoring" />
      </div>
      <p className="page-sub" style={{ marginTop: 0 }}>
        Everyone qualified into a campaign, strongest signal first. Scores are fixed at
        qualification, so the order stays stable while you work it.
      </p>

      <ErrorBanner error={error} />

      {/* The tiles describe the SELECTED state, from the server's own count — so
          they stay true when the view switches to Enrolled, and don't shrink to the
          page size when the fetch is capped. The band split can only be counted over
          what was loaded, so it says so. */}
      <div className="grid stat-grid" style={{ marginBottom: 18 }}>
        <Stat label={state === 'qualified' ? 'On the list' : (stateLabel || 'Members')}
          value={num(total)}
          sub={state === 'qualified' ? 'qualified, not yet sent to'
            : state === 'all' ? 'across every state' : `state: ${state}`} />
        <Stat label="Hot" value={num(counts.hot || 0)} tone="bad" sub={`score 70+${bandScope}`} />
        <Stat label="Warm" value={num(counts.warm || 0)} tone="warn" sub={`45–69${bandScope}`} />
        <Stat label="Cool" value={num(counts.cool || 0)} sub={`under 45${bandScope}`} />
      </div>

      <div className="filterbar">
        <Search value={f.q} onChange={(v) => set('q', v)} label="Find"
          placeholder="name, company, title, email…" />
        <label className="field">Show
          <select value={state} onChange={(e) => setState(e.target.value)}>
            {STATES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
        </label>
        <Pick label="Campaign" value={f.campaign} onChange={(v) => set('campaign', v)}
          options={campaigns} any="all campaigns" width={180} />
        <Pick label="Priority" value={f.band} onChange={(v) => set('band', v)}
          options={BANDS.map((b) => ({ ...b, count: counts[b.value] || 0 }))}
          any="any" width={110} />
        <Pick label="Persona" value={f.persona} onChange={(v) => set('persona', v)}
          options={personas} any="any" width={150} />
        <Pick label="Signal" value={f.kind} onChange={(v) => set('kind', v)}
          options={kinds} any="any" width={130} />
        <Toggle checked={f.hideSnoozed} onChange={(v) => set('hideSnoozed', v)}
          label="Hide snoozed"
          title="Members snoozed until a future date. They stay in the campaign." />
        <Toggle checked={f.hideInactive} onChange={(v) => set('hideInactive', v)}
          label="Hide paused"
          title="People paused or marked do-not-contact. The send gate already stops them; this hides them from the list." />
        <div className="grow" />
        <ColumnPicker value={cols} onChange={setColumns} />
      </div>

      <div className="row between" style={{ marginBottom: 12, flexWrap: 'wrap', gap: 10 }}>
        <FilterCount shown={rows.length} total={all.length} noun="contacts"
          active={dirty || !!sort}
          onReset={() => { setF(EMPTY_FILTERS); resetSort() }}
          note={sort
            ? `sorted by ${COLUMNS[sort.key]?.label || sort.key} ${sort.dir === 'asc' ? 'ascending' : 'descending'} — reset for the default account-diverse order`
            : null} />
        <span className="row" style={{ gap: 10 }}>
          {/* Sorting and filtering run over the rows in the browser, so a capped
              fetch would quietly answer "the weakest on the list" with "the weakest
              of the strongest 300". Say what is loaded, and offer the rest. */}
          {truncated && (
            <span className="hint">
              {num(all.length)} of {num(total)} loaded ·{' '}
              <button type="button" className="linklike" disabled={busy}
                onClick={() => setLimit(5000)}>load all {num(total)}</button>
            </span>
          )}
          {busy && <Spinner />}
          {/* The question this list raises — "is the score actually predicting?" — is
              answered on Analytics, so it links there rather than restating it. */}
          <Link className="ghost sm btn-link" to="/analytics?tab=funnel">
            Is this converting? →
          </Link>
        </span>
      </div>

      {!data ? <Spinner label="Loading…" /> : rows.length === 0 ? (
        <div className="empty">
          {all.length === 0
            ? 'Nothing on the call list yet. Qualify a campaign to populate it — a campaign’s accounts become this list, ordered by how strong their signal is.'
            : 'No contacts match these filters.'}
        </div>
      ) : (
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          <table className="dense" style={{ tableLayout: 'fixed', width: '100%',
            minWidth: Math.max(760, active.length * 118) }}>
            <thead><tr>
              {active.map((c) => (
                <SortTh key={c.id} id={c.id} label={c.label} width={c.w} title={c.title}
                  sortable={!!c.sort} dir={c.dir} sort={sort} onSort={toggle} />
              ))}
              <th style={{ width: 44 }} />
            </tr></thead>
            <tbody>
              {rows.map((m) => (
                <tr key={`${m.campaign_id}-${m.contact_id}`}
                  className={m.engagement_state && m.engagement_state !== 'active'
                    ? 'row-off' : ''}>
                  {active.map((c) => (
                    <td key={c.id} className={c.cls}>{c.render(m)}</td>
                  ))}
                  <td style={{ textAlign: 'right' }}>
                    <ContactActions member={m} onChanged={load} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
