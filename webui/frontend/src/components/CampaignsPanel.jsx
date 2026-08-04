import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from './ui.jsx'
import { useDemo, useCovers } from '../DemoContext.jsx'
import { StatusBadge, windowLabel, BandMix } from './campaignShared.jsx'
import CampaignDetail from './CampaignDetail.jsx'
import CampaignForm from './CampaignForm.jsx'
import MoneyRain from './MoneyRain.jsx'
import {
  useSort, sortRows, SortTh, Search, Pick, Toggle, FilterCount, facet, matcher,
} from './tableTools.jsx'

// A campaign is a DEFINED SET OF ACCOUNTS SHOWING SIGNAL OVER A TARGET WINDOW,
// worked through an ordered sequence where every step names the offer it carries.
// Membership is derived from the definition (signal query + window), never typed:
// rolling campaigns pick up accounts that fire mid-window on the hourly sweep, and
// discovery actively scans in-scope accounts to find more.
//
// Lives inside the Use view — starting a campaign IS how you put the AI SDR to
// work, so it belongs next to sourcing rather than on a page of its own.
//
// The index defaults to work-first order (active, then paused, then drafts, then
// everything finished — see batch_db.list_campaigns), which no column sort can
// reproduce, so "no sort" stays a reachable state and is where reset returns you.

// Sort accessors, keyed to the column ids in the header below.
const SORTS = {
  name: (c) => c.name,
  status: (c) => c.status,
  window: (c) => c.window_end,
  membership: (c) => c.membership_mode || 'rolling',
  accounts: (c) => c.counts?.accounts,
  contacts: (c) => c.counts?.members,
  mix: (c) => c.counts?.avg_score,
  bison: (c) => c.bison_campaign_id,
}

// "Live" is the question people actually have of this table — a paused or completed
// campaign is not sending — so it is a one-click toggle rather than three trips
// through the status dropdown.
const LIVE = new Set(['active'])

const EMPTY_FILTERS = { q: '', status: '', unbound: false, live: false }

export default function CampaignsPanel() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  // Deep link: /campaigns?open=<id> opens straight into that campaign, so the
  // funnel, Home and the hot list can all point AT a campaign rather than at the
  // list containing it.
  const [params, setParams] = useSearchParams()
  const [openId, setOpenId] = useState(() => {
    const v = Number(params.get('open'))
    return Number.isFinite(v) && v > 0 ? v : null
  })
  const [creating, setCreating] = useState(false)
  const [celebrating, setCelebrating] = useState(false)
  const [f, setF] = useState(EMPTY_FILTERS)
  const { sort, toggle, reset: resetSort } = useSort()
  const { profileId } = useDemo()
  const covers = useCovers('campaigns')

  function load() {
    api.campaigns().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])

  function close() {
    setOpenId(null)
    if (params.get('open')) { params.delete('open'); setParams(params, { replace: true }) }
    load()
  }

  if (openId) {
    return (
      <>
        {celebrating && <MoneyRain onDone={() => setCelebrating(false)} />}
        <CampaignDetail campaignId={openId} onBack={close} />
      </>
    )
  }

  const all = data?.campaigns || []
  const active = all.filter((c) => c.status === 'active')
  const members = all.reduce((a, c) => a + (c.counts?.members || 0), 0)
  const waiting = all.reduce((a, c) => a + (c.counts?.by_state?.qualified || 0), 0)
  const sig = data?.signal_counts || {}
  const sigTotal = Object.values(sig).reduce((a, b) => a + b, 0)

  function set(k, v) { setF((prev) => ({ ...prev, [k]: v })) }
  const statuses = facet(all, (c) => c.status || 'draft')
  const hit = matcher(f.q, (c) => [c.name, c.key, c.description])
  const rows = sortRows(all.filter((c) => (
    (!f.status || (c.status || 'draft') === f.status)
    && (!f.live || LIVE.has(c.status))
    // Unbound = no Bison campaign, so nothing this campaign qualifies can actually
    // be sent. Worth being able to isolate: it is the quietest way for a campaign
    // to do nothing.
    && (!f.unbound || !c.bison_campaign_id)
    && hit(c)
  )), sort, SORTS)
  const dirty = Object.keys(EMPTY_FILTERS).some((k) => f[k] !== EMPTY_FILTERS[k])

  return (
    <div>
      <p className="page-sub" style={{ marginTop: 0 }}>
        A campaign is a set of accounts showing signal over a target window, worked through a
        sequence where every step declares the offer it carries. Membership is derived from the
        definition — rolling campaigns pick up accounts that fire mid-window.
      </p>

      <ErrorBanner error={error} />
      {/* A demo must read as a working system, so a profile without campaign data
          gets the ordinary empty state below, never a capability warning. The
          banner is for live mode, where a missing table is real and actionable. */}
      {!profileId && data && data.available === false && (
        <div className="banner warn" style={{ marginBottom: 16 }}>
          Campaigns are not available for this dataset{data.error ? ` — ${data.error}` : ''}.
        </div>
      )}

      <div className="grid stat-grid" style={{ marginBottom: 20 }}>
        <Stat label="Campaigns" value={num(all.length)} sub={`${num(active.length)} active`} />
        <Stat label="Contacts in campaigns" value={num(members)} />
        <Stat label="Waiting to be worked" value={num(waiting)} tone={waiting ? 'warn' : undefined}
          sub="qualified, not yet sent to" />
        <Stat label="Signals fired (30d)" value={num(sigTotal)} accent
          sub={sigTotal
            ? Object.entries(sig).map(([k, v]) => `${v} ${k}`).join(' · ')
            : 'nothing observed yet'} />
      </div>

      {/* Building a campaign IS the product, so a demo has to be able to do it.
          Demo writes land in the profile's own DB and reach nothing external
          (see demo_mode.writable / demo_actions.py). This button was hidden while
          demo mode still refused every POST; that guard outlived the restriction. */}
      <div className="card-actions" style={{ marginTop: 0, marginBottom: 14 }}>
        <button className="primary sm" onClick={() => setCreating(true)}>New campaign</button>
        {profileId && (
          <span className="hint">
            Demo campaigns are saved to this profile only — nothing is sent and no
            credits are spent.
          </span>
        )}
      </div>

      {all.length > 0 && (
        <div className="filterbar">
          <Search value={f.q} onChange={(v) => set('q', v)} label="Find"
            placeholder="name or key…" width={190} />
          <Pick label="Status" value={f.status} onChange={(v) => set('status', v)}
            options={statuses} any="any status" width={140} />
          <Toggle checked={f.live} onChange={(v) => set('live', v)} label="Active only"
            title="Campaigns currently running. Paused, draft and completed ones send nothing." />
          <Toggle checked={f.unbound} onChange={(v) => set('unbound', v)} label="Not bound to a sender"
            title="No Bison campaign is attached, so nothing these accounts qualify for can be sent." />
          <div className="grow" />
          <FilterCount shown={rows.length} total={all.length} noun="campaigns"
            active={dirty || !!sort}
            onReset={() => { setF(EMPTY_FILTERS); resetSort() }}
            note={sort ? 'reset for work-first order' : null} />
        </div>
      )}

      {!data ? <Spinner label="Loading…" /> : rows.length === 0 ? (
        <div className="empty">
          {all.length > 0
            ? 'No campaigns match these filters.'
            : profileId && !covers
              ? 'Campaigns are not part of this demo profile.'
              : 'No campaigns yet. A campaign turns "these accounts showed signal this month" into a repeatable sequence with a declared offer per step.'}
        </div>
      ) : (
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 1020 }}>
            <thead><tr>
              <SortTh id="name" label="Campaign" width="22%" sort={sort} onSort={toggle} dir="asc" />
              <SortTh id="status" label="Status" width="8%" sort={sort} onSort={toggle} dir="asc" />
              <SortTh id="window" label="Window" width="16%" sort={sort} onSort={toggle} dir="asc"
                title="Sort by when the window closes — soonest first" />
              <SortTh id="membership" label="Membership" width="9%" sort={sort} onSort={toggle} dir="asc" />
              <SortTh id="accounts" label="Accounts" width="8%" sort={sort} onSort={toggle} />
              <SortTh id="contacts" label="Contacts" width="8%" sort={sort} onSort={toggle} />
              <SortTh id="mix" label="Priority mix" width="17%" sort={sort} onSort={toggle}
                title="Sort by average score" />
              <SortTh id="bison" label="Sends to" width="12%" sort={sort} onSort={toggle} dir="asc" />
            </tr></thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.campaign_id} className="clickable" onClick={() => setOpenId(c.campaign_id)}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{c.name}</div>
                    <div className="muted mono" style={{ fontSize: 11 }}>{c.key}</div>
                  </td>
                  <td><StatusBadge status={c.status} /></td>
                  <td className="muted" style={{ fontSize: 12 }}>
                    {windowLabel(c)}
                    {c.window_days_left != null && c.status === 'active' && (
                      <div style={{ color: c.window_days_left < 0 ? 'var(--red)' : 'var(--muted)' }}>
                        {c.window_days_left < 0
                          ? `closed ${-c.window_days_left}d ago`
                          : `${c.window_days_left}d left`}
                      </div>
                    )}
                  </td>
                  <td>
                    <span className="badge" title={c.membership_mode === 'rolling'
                      ? 'New qualifying accounts are added during the window'
                      : 'Membership was frozen at launch'}>
                      {c.membership_mode || 'rolling'}
                    </span>
                  </td>
                  <td>{num(c.counts?.accounts || 0)}</td>
                  <td>{num(c.counts?.members || 0)}</td>
                  <td><BandMix bands={c.counts?.by_band} avg={c.counts?.avg_score} /></td>
                  <td className="muted mono" style={{ fontSize: 12 }}>
                    {c.bison_campaign_id
                      ? `bison ${c.bison_campaign_id}`
                      : <span style={{ color: 'var(--amber)' }}>not bound</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {creating && (
        <CampaignForm
          onClose={() => setCreating(false)}
          onCreated={(id) => { setCreating(false); setCelebrating(true); setOpenId(id) }}
        />
      )}
    </div>
  )
}

