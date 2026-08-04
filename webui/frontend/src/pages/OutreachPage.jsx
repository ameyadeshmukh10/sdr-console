import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { Badge, Spinner, ErrorBanner, num, LINKEDIN_BLUE } from '../components/ui.jsx'
import { CampaignTags } from '../components/campaignShared.jsx'
import { BRAND } from '../theme.js'
import OutreachDetail from '../components/OutreachDetail.jsx'

// Pillar 4 — Transparency: browse/search the generated outreach, filter and
// group by persona / CTA play / status / company, open any lead's full copy.
const PAGE_SIZE = 50

// Sequence progress for one contact: filled = sent, hollow = staged or still draft.
// Two tracks because email and LinkedIn advance independently.
function Track({ sent, total, color, label }) {
  return (
    <span className="seq-track" title={`${label}: ${sent} of ${total} sent`}>
      {Array.from({ length: total }, (_, i) => (
        <span key={i} className={'seq-dot' + (i < sent ? ' on' : '')}
          style={i < sent ? { background: color, borderColor: color } : undefined} />
      ))}
    </span>
  )
}

function SeqCell({ seq, status }) {
  if (!seq) return <span className="muted">—</span>
  const { email_sent = 0, li_sent = 0, replied } = seq
  // Nothing enrolled and nothing sent = the copy is written but idle. Say that
  // rather than showing an empty track that looks like a stalled sequence.
  if (status !== 'enrolled' && !email_sent && !li_sent) {
    return <span className="muted" style={{ fontSize: 11.5 }}>not enrolled</span>
  }
  return (
    <span className="seq-cell">
      <Track sent={email_sent} total={4} color={BRAND.jade} label="Email" />
      <Track sent={li_sent} total={3} color={LINKEDIN_BLUE} label="LinkedIn" />
      {replied && <span className="badge seq-replied">replied</span>}
    </span>
  )
}

// Relative age of a generated sequence. Absolute timestamps add noise to a list
// this dense; "3w ago" is what the reader actually wants.
function ago(iso) {
  if (!iso) return '—'
  const d = (Date.now() - new Date(iso).getTime()) / 8.64e7
  if (!Number.isFinite(d) || d < 0) return '—'
  if (d < 1) return 'today'
  if (d < 2) return 'yesterday'
  if (d < 14) return `${Math.round(d)}d ago`
  if (d < 60) return `${Math.round(d / 7)}w ago`
  return `${Math.round(d / 30)}mo ago`
}

export default function OutreachPage() {
  // Seed from the URL so other views can deep-link a filtered slice — Home's signal
  // widget links here with ?company=<account> to show just that account's contacts.
  const [search] = useSearchParams()
  const [filters, setFilters] = useState(() => ({
    persona: search.get('persona') || '', cta: search.get('cta') || '',
    status: search.get('status') || '', q: '',
    company: search.get('company') || '', group_by: '',
    // Id filters arrive from a campaign or a contact link. They are held in the
    // same filter object so Clear resets them like anything else, rather than
    // being a hidden mode you can't get out of.
    campaign: search.get('campaign') || '', contact: search.get('contact') || '',
    // Findability for exclusions. Someone told us never to contact them a year
    // ago; when a new product lands you need to be able to LIST those people and
    // decide again. Without this the switch is one-way in practice.
    outreach: search.get('outreach') || '',
  }))
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [openId, setOpenId] = useState(null)

  // A refresh counter rather than a named loader: the fetch depends on filters and
  // page, and flipping a row's outreach switch has to re-run exactly that same
  // fetch. Bumping a dep keeps one code path instead of two that can drift.
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    setLoading(true)
    const params = { ...filters, page, page_size: PAGE_SIZE }
    Object.keys(params).forEach((k) => params[k] === '' && delete params[k])
    api.outreach(params)
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [filters, page, refresh])

  function setF(key, val) { setPage(1); setFilters((f) => ({ ...f, [key]: val })) }

  const facets = data?.facets || {}
  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  const facetOptions = (name) =>
    Object.entries(facets[name] || {}).map(([k, n]) => (
      <option key={k} value={k}>{k} ({n})</option>
    ))

  return (
    <div>
      <h1 className="page-title">Outreach</h1>
      <p className="page-sub">Every generated sequence, joined with contact + enrollment status. Click a row for the full copy.</p>

      {/* Arrived from a campaign or a contact. Shown as a removable chip rather
          than applied silently: a filtered list that doesn't say it is filtered is
          how someone concludes the copy is missing. */}
      {filters.outreach === 'off' && (
        <div className="banner info" style={{ marginBottom: 14 }}>
          Everyone outreach is switched off for. They still match campaigns and still
          show as targets — they are just never added. Flip the switch to bring
          someone back in.
        </div>
      )}
      {(filters.campaign || filters.contact) && (
        <div className="banner info" style={{ marginBottom: 14 }}>
          Showing {filters.contact ? 'one contact' : 'one campaign'}&rsquo;s copy only.
          <button className="linklike" style={{ marginLeft: 8 }}
            onClick={() => setFilters((f) => ({ ...f, campaign: '', contact: '' }))}>
            Show everything
          </button>
        </div>
      )}

      <div className="toolbar">
        <label className="field grow">Search
          {filters.company && (
            <span className="badge" style={{ alignSelf: 'center' }}>
              {filters.company}
              <button className="linklike" style={{ marginLeft: 6 }}
                onClick={() => setFilters((f) => ({ ...f, company: '' }))}>✕</button>
            </span>
          )}
          <input placeholder="name, email, company, signal…" value={filters.q}
            onChange={(e) => setF('q', e.target.value)} />
        </label>
        <label className="field">Persona
          <select value={filters.persona} onChange={(e) => setF('persona', e.target.value)}>
            <option value="">All</option>{facetOptions('persona')}
          </select>
        </label>
        <label className="field" title="Filters on the offer that appears in the written copy. Which offer each TOUCH carries is set per campaign, on its sequence.">Offer in copy
          <select value={filters.cta} onChange={(e) => setF('cta', e.target.value)}>
            <option value="">All</option>{facetOptions('cta_type')}
          </select>
        </label>
        <label className="field" title="Find people you have switched off, so you can switch them back on.">Outreach
          <select value={filters.outreach} onChange={(e) => setF('outreach', e.target.value)}>
            <option value="">All</option>
            <option value="on">On</option>
            <option value="off">Switched off</option>
          </select>
        </label>
        <label className="field">Status
          <select value={filters.status} onChange={(e) => setF('status', e.target.value)}>
            <option value="">All</option>{facetOptions('status')}
          </select>
        </label>
        <label className="field">Group by
          <select value={filters.group_by} onChange={(e) => setF('group_by', e.target.value)}>
            <option value="">— none —</option>
            <option value="persona">Persona</option>
            <option value="cta_type">Offer in copy</option>
            <option value="status">Status</option>
            <option value="company">Company</option>
          </select>
        </label>
      </div>

      <ErrorBanner error={error} />

      {data?.groups && (
        <div className="panel" style={{ marginBottom: 18 }}>
          <div className="section-h" style={{ margin: '0 0 12px' }}>Grouped by {filters.group_by} — {Object.keys(data.groups).length} groups</div>
          <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
            {Object.entries(data.groups).slice(0, 40).map(([k, n]) => (
              <span key={k} className="badge">{k || '—'}: {n}</span>
            ))}
          </div>
        </div>
      )}

      <div className="row between" style={{ marginBottom: 10 }}>
        <span className="muted">{loading ? <Spinner /> : `${num(data?.total || 0)} sequences`}</span>
      </div>

      <div className="panel" style={{ padding: 0 }}>
        {/* tableLayout: fixed — the Campaigns column pushed the table wider than
            its panel and SIGNAL ran off the right edge. Fixed widths make the
            long text truncate in place instead. */}
        <table className="fit" style={{ tableLayout: 'fixed', width: '100%' }}>
          <thead>
            <tr>
              <th style={{ width: '17%' }}>Name</th>
              <th style={{ width: '15%' }}>Company</th>
              <th style={{ width: '13%' }}>Campaigns</th>
              <th style={{ width: '12%' }}>Status</th>
              <th style={{ width: '14%' }}>Sequence</th>
              <th style={{ width: '22%' }}>Signal</th>
              <th style={{ width: '7%' }} title="Outreach on or off for this person">
                On
              </th>
            </tr>
          </thead>
          <tbody>
            {data?.items?.map((r) => (
              <tr key={r.contact_id}
                className={'clickable' + (r.engagement_state && r.engagement_state !== 'active'
                  ? ' row-off' : '')}
                onClick={() => setOpenId(r.contact_id)}>
                <td>
                  <div className="trunc-1">{r.first_name} {r.last_name}</div>
                  {r.persona && (
                    <div className="muted sub-line">{r.persona}</div>
                  )}
                </td>
                <td className="trunc">{r.company || <span className="muted">—</span>}</td>
                {/* Every campaign working this person. One row per contact, so an
                    overlap is visible here rather than only from inside whichever
                    campaign you happened to open — which is how someone gets
                    worked by three campaigns without anyone noticing. */}
                <td><CampaignTags campaigns={r.campaigns} compact /></td>
                {/* State is per MEMBERSHIP, not per person: someone can be enrolled
                    in one campaign and still only qualified in another. A single
                    status badge asserted one answer where there are several. */}
                <td><MemberStates r={r} /></td>
                <td>
                  <SeqCell seq={r.seq} status={r.status} />
                  {r.updated_at && (
                    <div className="muted sub-line">{ago(r.updated_at)}</div>
                  )}
                </td>
                <td className="muted trunc" title={r.signal}>{r.signal}</td>
                {/* stopPropagation: the row opens the copy drawer, and flipping the
                    switch must not also open it. */}
                <td onClick={(e) => e.stopPropagation()}>
                  <OutreachSwitch r={r} onChanged={() => setRefresh((n) => n + 1)} />
                </td>
              </tr>
            ))}
            {data && data.items.length === 0 && !loading && (
              <tr><td colSpan={7}><div className="empty">No matching sequences.</div></td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="pager">
        <button className="ghost sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>← Prev</button>
        <span className="muted">Page {page} / {totalPages}</span>
        <button className="ghost sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next →</button>
      </div>

      {openId && <OutreachDetail id={openId} onClose={() => setOpenId(null)} />}
    </div>
  )
}

// Where this person stands, per campaign.
//
// The old single status badge came from `contacts.status` — one value for the
// person — which is fine while everyone is in one campaign and wrong the moment
// they are not: enrolled in the funding push and still merely qualified in the
// closed-lost one is two facts, not one. Rolled up by state with counts, so the
// column stays narrow however many campaigns they are in.
//
// Falls back to the pipeline status for contacts in no campaign at all — that is
// genuinely all we know about them.
const STATE_TONE = {
  enrolled: 'status-enrolled', replied: 'status-enrolled',
  qualified: 'status-pending', generated: 'status-pending',
  removed: 'status-skipped',
}

function MemberStates({ r }) {
  const camps = r.campaigns || []
  if (!camps.length) {
    return (
      <span title="Not in any campaign">
        <Badge kind="status" value={r.status} />
      </span>
    )
  }
  const byState = camps.reduce((a, c) => {
    const k = c.state || 'qualified'
    a[k] = (a[k] || 0) + 1
    return a
  }, {})
  const title = camps.map((c) => `${c.name} — ${c.state}`).join('\n')
  return (
    <span className="row" style={{ gap: 4, flexWrap: 'wrap' }} title={title}>
      {Object.entries(byState).map(([state, n]) => (
        <span key={state} className={`badge ${STATE_TONE[state] || ''}`}>
          {camps.length > 1 ? `${n} ` : ''}{state}
        </span>
      ))}
    </span>
  )
}

// Outreach on or off, for the person.
//
// A switch rather than a "remove" button, because nothing is being deleted: the
// contact, their copy and their campaign memberships all stay exactly where they
// are. What changes is whether the pipeline may contact them — and that is
// reversible, so the control should look reversible.
//
// It writes `contacts.engagement_state`, the same field the enroll gate and
// qualification already consult (batch_db.suppressed_contact_ids), so switching it
// off genuinely stops sends rather than only hiding a row.
function OutreachSwitch({ r, onChanged }) {
  const [busy, setBusy] = useState(false)
  const off = r.engagement_state && r.engagement_state !== 'active'

  async function toggle() {
    setBusy(true)
    try {
      await api.updateEngagement({
        contact_id: r.contact_id,
        engagement_state: off ? 'active' : 'suppressed',
      })
      onChanged()
    } finally { setBusy(false) }
  }

  const title = off
    ? `Outreach is OFF${r.engagement_state === 'paused' && r.paused_until
      ? ` until ${r.paused_until}` : ''}${r.engagement_note ? ` — ${r.engagement_note}` : ''}.`
      + ' They stay in the campaigns they are already in, nothing sends, and future'
      + ' campaigns will not add them — but they still show as a match wherever they'
      + ' qualify. Click to turn back on.'
    : 'Outreach is on. Click to switch it off: no sends, and no future campaign adds'
      + ' them — they stay visible as a target, just not contacted. Enforced at the'
      + ' send gate, not just here.'

  return (
    <button type="button" disabled={busy} onClick={toggle} title={title}
      aria-pressed={!off} aria-label={off ? 'Turn outreach on' : 'Turn outreach off'}
      className={'switch' + (off ? ' off' : '')}>
      <span className="knob" />
    </button>
  )
}
