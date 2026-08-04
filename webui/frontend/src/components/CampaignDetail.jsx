import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from './ui.jsx'
import { StatusBadge, windowLabel, ScoreBadge, Momentum, Channels,
  CampaignTags, Steps, Money, ChannelPlan, ChannelChips } from './campaignShared.jsx'
import SequenceEditor from './SequenceEditor.jsx'
import DiscoveryPanel from './DiscoveryPanel.jsx'
import EnrichPanel from './EnrichPanel.jsx'
import ContactLink, { Phone } from './ContactLink.jsx'
import AudienceStep from './AudienceStep.jsx'
import EvergreenReview from './EvergreenReview.jsx'
import CampaignOutreach from './CampaignOutreach.jsx'
import CampaignReplies from './CampaignReplies.jsx'
import CampaignExclusions from './CampaignExclusions.jsx'
import { Link } from 'react-router-dom'

// One campaign, end to end: what defines it, who it caught, the sequence with its
// per-step offers, and the lifecycle controls.
//
// The "matches now" panel is the load-bearing bit of the definition tab: a signal
// query is abstract, and the only honest way to show what it means is to run it
// read-only and say how many accounts it currently catches and why the rest were
// skipped.

// The workflow, in the order the work actually happens. Numbered because the whole
// point of the campaign model is that it IS a sequence of decisions: who, then find
// more of them, then what you say, then who to work first.
// The workflow, in the order the work actually happens. Numbered because a
// campaign IS an ordered set of decisions, not four unrelated screens.
const TABS = [
  { id: 'audience', label: 'Audience' },
  { id: 'definition', label: 'Find accounts' },
  { id: 'sequence', label: 'Sequence & offers' },
  { id: 'members', label: 'Call list' },
  // The two halves of what actually happened: what we said, and what came back.
  { id: 'outreach', label: 'Outreach' },
  { id: 'replies', label: 'Replies' },
]

export default function CampaignDetail({ campaignId, onBack }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [tab, setTab] = useState('audience')
  const [busy, setBusy] = useState(null)

  function load() {
    api.campaign(campaignId)
      .then((d) => { if (d.error) setError(d.error); else { setData(d); setError(null) } })
      .catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [campaignId])

  async function act(kind, fn) {
    setBusy(kind); setError(null)
    try { await fn(); load() } catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  if (!data) {
    return (
      <div>
        <button className="ghost sm" onClick={onBack}>← Campaigns</button>
        <ErrorBanner error={error} />
        {!error && <Spinner label="Loading…" />}
      </div>
    )
  }

  const c = data.campaign
  const counts = data.counts || {}
  const preview = data.match_preview || {}
  const enrolled = counts.by_state?.enrolled || 0
  const qualified = counts.by_state?.qualified || 0

  return (
    <div>
      <button className="ghost sm" onClick={onBack}>← Campaigns</button>

      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start', marginTop: 10 }}>
        <div>
          <h1 className="page-title" style={{ marginBottom: 4 }}>{c.name}</h1>
          <div className="row" style={{ gap: 8 }}>
            <StatusBadge status={c.status} />
            {c.campaign_type === 'inbound' && (
              <span className="badge badge-inbound"
                title="They came to us. Copy never cold-opens, and the pipeline reports as influenced rather than created.">
                inbound
              </span>
            )}
            <span className="badge">{c.membership_mode || 'rolling'}</span>
            <ChannelChips value={c.channels} />
            {c.evergreen ? (
              <span className="badge" title={
                `Re-runs in cycles; you're asked to confirm the messaging every ${
                  c.evergreen_interval_days || 30} days`}>
                evergreen · cycle {c.cycle || 1}
              </span>
            ) : null}
            <span className="muted" style={{ fontSize: 12 }}>{windowLabel(c)}</span>
            {c.window_days_left != null && c.status === 'active' && (
              <span className="muted" style={{ fontSize: 12 }}>
                · {c.window_days_left < 0 ? 'window closed' : `${c.window_days_left}d left`}
              </span>
            )}
          </div>
          {data.audience_desc && (
            <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              Audience: <b>{data.audience_desc}</b>
            </div>
          )}
          {c.description && <p className="page-sub" style={{ marginTop: 8 }}>{c.description}</p>}
          {/* The direction agreed for this campaign. Shown here because it is what
              the copy generator is working from — a sequence arguing something
              nobody can see on the campaign is the thing this replaces. */}
          {c.brief && (
            <div className="brief-note">
              <span className="from-brief">✦</span>
              <span>{c.brief}</span>
            </div>
          )}
        </div>
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          {/* Forward link to the same campaign's numbers. The definition and the
              result are two views of one thing and should be one click apart. */}
          <Link className="ghost sm btn-link" to={`/analytics?tab=funnel&campaign=${campaignId}`}>
            How is it doing? →
          </Link>
          <Lifecycle c={c} busy={busy} act={act} />
        </div>
      </div>

      <ErrorBanner error={error} />

      <div className="grid stat-grid" style={{ marginBottom: 18, marginTop: 14 }}>
        <Stat label="Accounts" value={num(counts.accounts || 0)}
          sub={c.target_accounts ? `cap ${num(c.target_accounts)}` : 'uncapped'} />
        <Stat label="Contacts" value={num(counts.members || 0)} />
        <Stat label="Qualified, not yet sent" value={num(qualified)} tone={qualified ? 'warn' : undefined} />
        <Stat label="Enrolled" value={num(enrolled)} tone={enrolled ? 'good' : undefined}
          sub={c.bison_campaign_id ? `bison ${c.bison_campaign_id}` : 'no Bison campaign bound'} />
      </div>

      {/* A pending review comes FIRST and outside the tabs: the campaign is not
          sending until it is answered, so it is not one option among four. */}
      {c.review_state === 'pending' && (
        <EvergreenReview campaign={c} counts={counts} onRelaunched={load} />
      )}

      <Steps steps={TABS} current={tab} onSelect={setTab} />

      {tab === 'sequence' && (
        <>
          {/* The channel plan sits with the sequence because it is the same
              decision at a different altitude: which channels at all, then what
              each touch says. */}
          <div className="panel" style={{ marginBottom: 16 }}>
            <div className="card-h">
              <div>
                <h3>How it reaches people</h3>
                <div className="card-note">
                  Direct touches go to the people worth one; advertising covers the
                  rest of the buying group so the direct ones land warmer.
                </div>
              </div>
            </div>
            <ChannelPlan
              value={c.channels || { email: true, linkedin: true, ads: false }}
              reach={data.ad_reach}
              disabled={busy === 'channels'}
              onChange={(v) => act('channels', () =>
                api.updateCampaign(campaignId, { channels: v }))} />
          </div>
          <SequenceEditor campaignId={campaignId} steps={data.steps || []} ctas={data.ctas || []}
            planPrompt={data.plan_prompt} onChanged={load} />
        </>
      )}

      {tab === 'audience' && (
        <>
          <AudienceStep campaign={c} onSaved={load} />
          {/* Who is in scope, and — right underneath — who is deliberately out
              of it. An exclusion nobody can find is permanent by accident. */}
          <CampaignExclusions campaignId={campaignId} onChanged={load} />
        </>
      )}

      {tab === 'definition' && (
        <>
          <DiscoveryPanel campaignId={campaignId} discovery={data.discovery} onDone={load} />
          <EnrichPanel campaignId={campaignId} enrichment={data.enrichment} onDone={load} />
          <DefinitionTab c={c} preview={preview} busy={busy} act={act} onChanged={load} />
        </>
      )}

      {tab === 'outreach' && <CampaignOutreach campaignId={campaignId} />}
      {tab === 'replies' && <CampaignReplies campaignId={campaignId} />}

      {tab === 'members' && (
        <MembersTab members={data.members || []} counts={counts} busy={busy}
          onRescore={() => act('rescore', () => api.rescoreCampaign(campaignId))} />
      )}
    </div>
  )
}

// Status transitions, stated as what they do rather than as raw status names.
// Launching is what turns the hourly rolling sweep on for this campaign.
function Lifecycle({ c, busy, act }) {
  const set = (status) => act(status, () => api.updateCampaign(c.campaign_id, { status }))
  return (
    <div className="row" style={{ gap: 8 }}>
      {c.status === 'draft' && (
        <button className="primary sm" disabled={busy} onClick={() => set('active')}
          title="Start qualifying accounts into this campaign on the hourly sweep">
          {busy === 'active' ? <Spinner /> : 'Launch'}
        </button>
      )}
      {c.status === 'active' && (
        <button className="ghost sm" disabled={busy} onClick={() => set('paused')}
          title="Stop adding new accounts. Nothing already enrolled is stopped.">
          {busy === 'paused' ? <Spinner /> : 'Pause'}
        </button>
      )}
      {c.status === 'paused' && (
        <button className="primary sm" disabled={busy} onClick={() => set('active')}>Resume</button>
      )}
      {['active', 'paused'].includes(c.status) && (
        <button className="ghost sm" disabled={busy} onClick={() => set('completed')}>Complete</button>
      )}
    </div>
  )
}

function DefinitionTab({ c, preview, busy, act, onChanged }) {
  const q = c.signal_query || {}
  const skipped = preview.skipped || {}
  const skippedList = Object.entries(skipped).filter(([, v]) => v > 0)
  return (
    <div className="grid" style={{ gap: 16, gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)' }}>
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>What defines membership</h3>
        <Row k="Window" v={windowLabel(c)} />
        <Row k="Membership" v={c.membership_mode === 'rolling'
          ? 'Rolling — new qualifiers added hourly while the window is open'
          : 'Snapshot — frozen at launch'} />
        <Row k="Signal types" v={(q.kinds || []).join(', ') || 'any'} />
        {q.require_recent && <Row k="Account news" v="real dated events only (no fallback anchors)" />}
        {q.hiring_sales_min != null && <Row k="Hiring" v={`at least ${q.hiring_sales_min} open sales roles`} />}
        {(q.tech_playbook || []).length > 0 && <Row k="Tech play" v={q.tech_playbook.join(' or ')} />}
        <Row k="Personas" v={(q.personas || []).join(', ') || 'all'} />
        <Row k="Motion" v={q.motion || 'outbound'} />
        <Row k="Fit gate" v={
          [q.min_score != null ? `score ≥ ${q.min_score}` : null,
            q.require_senior ? 'senior buyers only' : null]
            .filter(Boolean).join(' · ') || 'none — every contact at a qualifying account'} />
        <Row k="Account cap" v={c.target_accounts ? num(c.target_accounts) : 'none'} />
        <Row k="Last qualified" v={c.last_qualified_at || 'never'} />
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>What it matches now</h3>
        {preview.error ? (
          <div className="banner error">{preview.error}</div>
        ) : (
          <>
            <div className="row" style={{ gap: 20, marginBottom: 10 }}>
              <div>
                <div className="muted" style={{ fontSize: 11 }}>ACCOUNTS WITH SIGNAL</div>
                <div style={{ fontSize: 22, fontWeight: 700 }}>{num(preview.accounts_matched || 0)}</div>
              </div>
              <div>
                <div className="muted" style={{ fontSize: 11 }}>NEW CONTACTS TO ADD</div>
                <div style={{ fontSize: 22, fontWeight: 700,
                  color: preview.candidates ? 'var(--green)' : 'var(--muted)' }}>
                  {num(preview.candidates || 0)}
                </div>
              </div>
            </div>
            {/* Matched, but switched off. Kept separate from the skip reasons: these
                people are part of the addressable set and would be added the moment
                outreach is turned back on. */}
            {preview.off_count > 0 && (
              <div className="off-targets">
                <b>{num(preview.off_count)}</b> more match but have outreach switched
                off, so they stay out of this campaign.
                {(preview.off_targets || []).length > 0 && (
                  <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>
                    {preview.off_targets.slice(0, 4)
                      .map((t) => t.name || t.company || t.domain).join(' · ')}
                    {preview.off_count > 4 ? ` +${preview.off_count - 4} more` : ''}
                  </div>
                )}
              </div>
            )}
            {skippedList.length > 0 && (
              <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
                Not added: {skippedList.filter(([k]) => k !== 'suppressed').map(([k, v]) =>
                  `${v} ${({ below_fit: 'below the fit bar', not_senior: 'not senior enough' }[k]
                    || k.replace(/_/g, ' '))}`).join(', ')}.
              </div>
            )}
            {(preview.preview || []).length > 0 && (
              <div style={{ maxHeight: 220, overflowY: 'auto', marginBottom: 10 }}>
                <table className="dense" style={{ width: '100%' }}>
                  <tbody>
                    {preview.preview.slice(0, 20).map((p) => (
                      <tr key={p.contact_id}>
                        <td style={{ fontSize: 12 }}>{p.name}</td>
                        <td className="muted" style={{ fontSize: 12 }}>{p.company}</td>
                        <td><span className="badge">{p.signal_kind}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <button className="primary sm" disabled={busy || !preview.candidates}
              onClick={() => act('qualify', async () => {
                await api.qualifyCampaign(c.campaign_id, {}); onChanged()
              })}>
              {busy === 'qualify' ? <Spinner /> : `Add ${num(preview.candidates || 0)} contacts now`}
            </button>
            <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
              Adding contacts only records membership. Copy generation and enrollment stay
              separate, explicit steps.
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function Row({ k, v }) {
  return (
    <div className="row" style={{ justifyContent: 'space-between', gap: 12, padding: '5px 0',
      borderBottom: '1px solid var(--border)', fontSize: 13 }}>
      <span className="muted">{k}</span>
      <span style={{ textAlign: 'right' }}>{v}</span>
    </div>
  )
}

// This campaign's slice of the call list — priority-ordered, same as the
// cross-campaign view under Use. Rescoring is explicit: scores are frozen at
// qualification so a list stays stable while it is being worked.
function MembersTab({ members, counts, busy, onRescore }) {
  if (members.length === 0) {
    return (
      <div className="empty">
        No accounts in this campaign yet. Use <b>Find accounts</b> to scan for signal, then
        qualify — the accounts that match become this call list, ordered by signal strength.
      </div>
    )
  }
  const b = counts?.by_band || {}
  return (
    <div>
      <div className="card-h" style={{ marginBottom: 12 }}>
        <p className="card-note" style={{ marginTop: 0 }}>
          Strongest signal first. Scores are fixed at qualification, so the order holds
          while you work it.
        </p>
        <button className="ghost sm" disabled={busy} onClick={onRescore}
          title="Recompute priorities against the signals visible now">
          {busy === 'rescore' ? <Spinner /> : 'Rescore'}
        </button>
      </div>
      <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
        <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 980 }}>
          <thead><tr>
            <th style={{ width: '7%' }}>$</th>
            <th style={{ width: '6%' }}>Score</th>
            <th style={{ width: '6%' }}>Trend</th>
            <th style={{ width: '14%' }}>Name</th>
            <th style={{ width: '11%' }}>Company</th>
            <th style={{ width: '9%' }}>Phone</th>
            <th style={{ width: '9%' }}>Channels</th>
            <th style={{ width: '14%' }}>Campaigns</th>
            <th style={{ width: '8%' }}>State</th>
            <th style={{ width: '22%' }}>Why they qualified</th>
          </tr></thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.contact_id}>
                <td><Money value={m.money} detail={m.score_detail} /></td>
                <td><ScoreBadge score={m.priority_score} band={m.score_band} detail={m.score_detail} /></td>
                <td><Momentum value={m.momentum} /></td>
                <td><ContactLink contact={m} /></td>
                <td className="muted">{m.company || m.domain}</td>
                <td><Phone contact={m} /></td>
                <td><Channels value={m.channels} /></td>
                <td><CampaignTags campaigns={m.all_campaigns} current={m.campaign_id} /></td>
                <td><span className={`badge status-${m.state}`}>{m.state}</span></td>
                <td className="muted">
                  <span className="clamp2" title={m.signal_snapshot?.summary}>
                    {m.signal_snapshot?.summary || '—'}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
