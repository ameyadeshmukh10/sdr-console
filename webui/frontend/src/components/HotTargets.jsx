import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, ErrorBanner, num } from './ui.jsx'
import Addon from './Addon.jsx'
import { Money } from './campaignShared.jsx'
import {
  useSort, sortRows, SortTh, Search, Pick, Toggle, FilterCount, facet, matcher,
} from './tableTools.jsx'

// The daily hot-target report: the 20 ACCOUNTS that best fit whatever campaigns are
// currently active.
//
// Account-level on purpose — a rep plans a day around accounts and then works the
// buying committee inside each; the call list already handles contact ordering.
//
// Served from a snapshot refreshed once a day rather than computed per request. A
// target list that reshuffled between two page loads is not something anyone can
// plan against, and "the list I was given this morning" has to still be that list
// at 4pm.
//
// Filtering and sorting are a VIEW over that snapshot, never a rebuild of it. The #
// column keeps the rank the snapshot assigned, so re-sorting by buyers or by $ does
// not renumber the list and quietly claim a different account was today's top
// target — "we're number 3 on your list" has to stay true all day.

// Sort accessors. `fit` is the snapshot's own ranking, so it restores the default.
const SORTS = {
  rank: (a) => a.rank,
  fit: (a) => a.fit,
  company: (a) => a.company || a.domain,
  money: (a) => a.money?.level ?? null,
  contacts: (a) => a.contacts,
  best_score: (a) => a.best_score,
  campaign_name: (a) => a.campaign_name,
}

const EMPTY_FILTERS = { q: '', campaign: '', untouched: false, warming: false }

export default function HotTargets() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [f, setF] = useState(EMPTY_FILTERS)
  const { sort, toggle, reset: resetSort } = useSort()

  function load() {
    api.hotList().then((d) => { setData(d); setError(null) }).catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])

  async function refresh() {
    setBusy(true); setError(null)
    try { await api.refreshHotList(); load() }
    catch (e) {
      setError(e.status === 409
        ? 'Demo mode is read-only — the snapshot shown is the profile’s own.'
        : e.message)
    } finally { setBusy(false) }
  }

  function set(k, v) { setF((prev) => ({ ...prev, [k]: v })) }

  // The snapshot's order IS the ranking, so the position is stamped on the row once
  // and travels with it through every filter and sort.
  const all = (data?.accounts || []).map((a, i) => ({ ...a, rank: i + 1 }))
  const warming = all.filter((a) => (a.momentum_sum || 0) > 0).length
  const untouched = all.filter((a) => !a.enrolled).length

  const campaigns = facet(all, (a) => a.campaign_name)
  const hit = matcher(f.q, (a) => [a.company, a.domain, a.campaign_name,
    ...(a.reasons || [])])
  const rows = sortRows(all.filter((a) => (
    (!f.campaign || a.campaign_name === f.campaign)
    && (!f.untouched || !a.enrolled)
    && (!f.warming || (a.momentum_sum || 0) > 0)
    && hit(a)
  )), sort, SORTS)
  const dirty = Object.keys(EMPTY_FILTERS).some((k) => f[k] !== EMPTY_FILTERS[k])

  return (
    <div>
      <div className="row" style={{ gap: 8, marginBottom: 2 }}>
        <Addon id="lead-scoring" />
      </div>
      <p className="page-sub" style={{ marginTop: 0 }}>
        The {num(data?.size || 20)} accounts that best fit your active campaigns, refreshed daily.
        Fit combines the account’s strongest contact, how much of the buying committee is
        mapped, and whether its signal is warming.
      </p>

      <ErrorBanner error={error} />

      {/* The tiles describe the SNAPSHOT, not the filtered view — they are what the
          day's list contains, and re-counting them as you narrow would leave no way
          to see the whole. The filtered count sits on the table instead. */}
      <div className="grid stat-grid" style={{ marginBottom: 18 }}>
        <Stat label="On the list" value={num(all.length)} accent
          sub={data?.pool ? `from ${num(data.pool)} active-campaign accounts` : null} />
        <Stat label="Signal warming" value={num(warming)} tone={warming ? 'good' : undefined} />
        <Stat label="Not yet contacted" value={num(untouched)}
          tone={untouched ? 'warn' : undefined} />
        <Stat label="Generated"
          value={data?.generated_at ? data.generated_at.slice(0, 10) : '—'}
          sub={data?.stale ? 'stale — refresh due' : 'today’s list'}
          tone={data?.stale ? 'warn' : undefined} />
      </div>

      <div className="filterbar">
        <Search value={f.q} onChange={(v) => set('q', v)} label="Find"
          placeholder="account or domain…" width={190} />
        <Pick label="Campaign" value={f.campaign} onChange={(v) => set('campaign', v)}
          options={campaigns} any="all campaigns" width={190} />
        <Toggle checked={f.untouched} onChange={(v) => set('untouched', v)}
          label="Not yet contacted"
          title="Accounts with nobody enrolled yet — the ones still entirely open" />
        <Toggle checked={f.warming} onChange={(v) => set('warming', v)}
          label="Warming only"
          title="Accounts whose signal has strengthened since they were last scored" />
        <div className="grow" />
        <FilterCount shown={rows.length} total={all.length} noun="accounts"
          active={dirty || !!sort}
          onReset={() => { setF(EMPTY_FILTERS); resetSort() }} />
      </div>

      <div className="row" style={{ gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
        <button className="ghost sm" disabled={busy} onClick={refresh}>
          {busy ? <Spinner /> : '↻ Rebuild now'}
        </button>
        <span className="muted" style={{ fontSize: 12, alignSelf: 'center' }}>
          Rebuilds automatically once every 24 hours. Filtering and sorting are a view
          over today’s snapshot — <b>#</b> keeps the rank it was given this morning.
        </span>
      </div>

      {!data ? <Spinner label="Loading…" /> : rows.length === 0 ? (
        <div className="empty">
          {all.length === 0
            ? <>No hot targets yet — the list is built from accounts in <b>active</b> campaigns.
              Launch a campaign and qualify it to populate this.</>
            : 'No accounts match these filters.'}
        </div>
      ) : (
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          <table className="dense" style={{ tableLayout: 'fixed', width: '100%', minWidth: 940 }}>
            <thead><tr>
              <SortTh id="rank" label="#" width="5%" sort={sort} onSort={toggle} dir="asc"
                title="Today’s rank. Sorting another column does not renumber it." />
              <SortTh id="fit" label="Fit" width="7%" sort={sort} onSort={toggle} />
              <SortTh id="company" label="Account" width="22%" sort={sort} onSort={toggle} dir="asc" />
              <SortTh id="money" label="$" width="7%" sort={sort} onSort={toggle} />
              <SortTh id="contacts" label="Buyers" width="8%" sort={sort} onSort={toggle}
                title="Sort by how much of the buying committee is mapped" />
              <SortTh id="best_score" label="Best score" width="8%" sort={sort} onSort={toggle} />
              <SortTh id="campaign_name" label="Campaign" width="18%" sort={sort} onSort={toggle} dir="asc" />
              <th style={{ width: '30%' }}>Why it’s here</th>
            </tr></thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.domain}>
                  {/* The snapshot rank, not the row position: sorted by buyers, the
                      third row is still whichever account was #12 this morning. */}
                  <td className="muted">{a.rank}</td>
                  <td>
                    <span className="badge" style={{
                      color: a.rank <= 5 ? 'var(--red)' : a.rank <= 12 ? 'var(--amber)' : 'var(--muted)',
                      borderColor: a.rank <= 5 ? 'var(--red)' : a.rank <= 12 ? 'var(--amber)' : 'var(--border-strong)',
                    }}>{Math.round(a.fit)}</span>
                  </td>
                  <td>
                    <div style={{ fontWeight: 600 }}>{a.company || a.domain}</div>
                    <div className="muted mono" style={{ fontSize: 11 }}>{a.domain}</div>
                  </td>
                  <td><Money value={a.money} /></td>
                  <td>{num(a.contacts)}{a.hot ? <span className="muted"> ({a.hot} hot)</span> : null}</td>
                  <td>{a.best_score == null ? '—' : Math.round(a.best_score)}</td>
                  <td className="muted" style={{ fontSize: 12 }}>
                    <span className="clamp2">{a.campaign_name || '—'}</span>
                    {a.campaigns > 1 && (
                      <span className="badge" style={{ fontSize: 10, marginLeft: 4 }}>
                        +{a.campaigns - 1}
                      </span>
                    )}
                  </td>
                  <td className="muted" style={{ fontSize: 12 }}>
                    {(a.reasons || []).join(' · ') || '—'}
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
