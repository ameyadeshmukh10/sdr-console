import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import Addon from './Addon.jsx'

// Find the REST of the buyer group, via Clay.
//
// Discovery scans accounts we already have contacts at. This is the other
// direction: at those same accounts, find the buyers we DON'T hold. That is what
// turns "the 3 people we happen to have at Acme" into a mapped buying committee —
// and a mapped committee is also what makes an ad audience worth buying.
//
// Clay charges per company searched AND per email revealed, so nothing here spends
// without showing the floor cost first. The estimate is explicitly a floor, not a
// quote: the reveal count is unknown until the search returns.

export default function EnrichPanel({ campaignId, enrichment, onDone }) {
  const [limit, setLimit] = useState(25)
  const [perCompany, setPerCompany] = useState(3)
  const [addToCampaign, setAddToCampaign] = useState(false)
  const [job, setJob] = useState(null)
  const [estimate, setEstimate] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!job || job.status !== 'running') return
    const t = setInterval(async () => {
      try {
        const j = await api.enrichStatus(job.job_id)
        setJob(j)
        if (j.status !== 'running') onDone()
      } catch (e) { setJob(null); setError(e.message) }
    }, 2500)
    return () => clearInterval(t)
  }, [job?.job_id, job?.status])

  const e = enrichment || {}
  const running = job?.status === 'running' || e.running
  const accounts = e.accounts || 0

  async function doEstimate() {
    setBusy('est'); setError(null)
    try { setEstimate(await api.enrichCampaign(campaignId, { dry_run: true, limit })) }
    catch (err) { setError(err.message) } finally { setBusy(null) }
  }

  async function doRun() {
    setBusy('run'); setError(null); setEstimate(null)
    try {
      setJob(await api.enrichCampaign(campaignId, {
        limit, per_company_cap: perCompany, add_to_campaign: addToCampaign,
      }))
    } catch (err) {
      setError(err.status === 409 ? 'Enrichment is already running for this campaign.' : err.message)
    } finally { setBusy(null) }
  }

  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="card-h">
        <div>
          <h3>Find more of the buyer group <Addon id="enrichment-connector" /></h3>
          <div className="card-note">
            {e.error
              ? `Scope unavailable — ${e.error}`
              : accounts > 0
                ? <>Clay can search <b>{num(accounts)}</b> of this campaign's accounts for buyers we
                  don't hold, thinnest buying committee first.</>
                : 'No accounts in this campaign yet — qualify some first.'}
          </div>
        </div>
        <div className="card-meta">
          {e.last_run_at ? <>Last enriched {e.last_run_at.slice(0, 10)}</> : 'Never enriched'}
        </div>
      </div>

      <ErrorBanner error={error} />

      {(e.sample || []).length > 0 && !running && (
        <div className="hint" style={{ marginTop: 12 }}>
          Thinnest first: {e.sample.slice(0, 5).map((s) => `${s.domain} (${s.held})`).join(', ')}
          {e.sample.length > 5 && ' …'}
        </div>
      )}

      <div className="card-actions">
        <label className="f">
          Search{' '}
          <select value={limit} disabled={running} onChange={(ev) => setLimit(Number(ev.target.value))}>
            {[10, 25, 50, 100].map((n) => <option key={n} value={n}>{n} accounts</option>)}
          </select>
        </label>
        <label className="f">
          Keep up to{' '}
          <input type="number" min="1" max="10" className="f-num" value={perCompany}
            disabled={running} onChange={(ev) => setPerCompany(Number(ev.target.value))} />
          {' '}per account
        </label>
        <label className="f-check">
          <input type="checkbox" checked={addToCampaign} disabled={running}
            onChange={(ev) => setAddToCampaign(ev.target.checked)} />
          <span>Add them straight into this campaign</span>
        </label>
      </div>

      <div className="card-actions">
        <button className="ghost sm" disabled={busy || running || !accounts} onClick={doEstimate}>
          {busy === 'est' ? <Spinner /> : 'Estimate cost'}
        </button>
        <button className="primary sm" disabled={busy || running || !accounts} onClick={doRun}>
          {busy === 'run' ? <Spinner /> : `Find buyers at ${Math.min(limit, accounts)} accounts`}
        </button>
        <span className="hint">Clay bills per account searched and per email revealed.</span>
      </div>

      {!addToCampaign && (
        <div className="hint" style={{ marginTop: 10 }}>
          New contacts are created in HubSpot and the pipeline but NOT sequenced — qualify the
          campaign when you've reviewed them.
        </div>
      )}

      {estimate && (
        <div className="banner info" style={{ marginTop: 12, marginBottom: 0 }}>
          <b>{num(estimate.accounts)} accounts</b> would be searched — at least{' '}
          <b>{num(estimate.credits_floor)} Clay credits</b>. {estimate.note}
        </div>
      )}

      {running && (
        <div className="hint" style={{ marginTop: 14 }}>
          <Spinner label={job?.total
            ? `Searching ${job.done}/${job.total}${job.current ? ` — ${job.current}` : ''}`
            : 'Starting…'} />
        </div>
      )}

      {job?.status === 'done' && (
        <div className={'banner ' + (job.unavailable ? 'warn' : 'info')}
          style={{ marginTop: 12, marginBottom: 0 }}>
          {job.unavailable ? job.unavailable : (
            <>Searched <b>{num(job.accounts)}</b> accounts, found <b>{num(job.found)}</b> contacts,
              created <b>{num(job.created)}</b>
              {job.added_to_campaign ? <>, added <b>{num(job.added_to_campaign)}</b> to the campaign</> : ''}.
              {' '}Spent <b>{num(job.credits)}</b> Clay credits.
              {job.note && <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{job.note}</div>}
            </>
          )}
          {job.errors?.length > 0 && (
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {job.errors.length} account(s) failed — first: {job.errors[0]}
            </div>
          )}
        </div>
      )}
      {job?.status === 'error' && (
        <div className="banner error" style={{ marginTop: 12, marginBottom: 0 }}>
          Enrichment failed: {job.error}
        </div>
      )}
    </div>
  )
}
