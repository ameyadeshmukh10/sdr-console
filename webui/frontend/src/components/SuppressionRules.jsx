import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Stat, Spinner, num } from './ui.jsx'

// Unenrollment & suppression rules — the safety gate that keeps the AI SDR off
// contacts RevOps has flagged. Lives with the Pipeline view because it is an
// operational control over enrollment (with Run now / Dry run), not configuration:
// Setup explains how the gate works, this runs it.
//
// Self-contained: fetches its own status and owns its run state, so any page can
// mount it with no props.

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

export default function SuppressionRules({ heading = 'Unenrollment & suppression rules' }) {
  const [unenroll, setUnenroll] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)

  useEffect(() => {
    // Non-fatal: the page renders fine without it (e.g. an older backend).
    api.unenrollStatus().then(setUnenroll).catch(() => {})
  }, [])

  // Kick a sweep (or dry run), then poll until the global run flag clears and
  // refresh the rule cards — same pattern as the Analytics attribution sync.
  async function runCheck(dryRun) {
    setBusy(true)
    setMsg(dryRun ? 'Starting dry run…' : 'Starting unenrollment check…')
    try {
      await api.unenrollRun({ dry_run: !!dryRun })
    } catch (e) {
      // 409 = a check is already running (e.g. the background sweeper) — keep polling it.
      if (e.status !== 409) {
        setMsg(`Unenrollment check failed to start: ${e.message}`)
        setBusy(false)
        return
      }
    }
    setMsg('Unenrollment check running — sweeping flagged contacts across both channels…')
    for (let i = 0; i < 120; i++) {          // up to ~10 min
      await new Promise((r) => setTimeout(r, 5000))
      try {
        const s = await api.unenrollStatus()
        if (s.running && s.progress) setMsg(`Running — ${s.progress}`)
        if (!s.running) {
          setUnenroll(s)
          if (dryRun) {
            // Dry runs never touch last_run — their summary comes via last_result.
            const d = s.last_result
            setMsg(d && typeof d === 'object' && d.dry_run
              ? (d.ok === false
                  ? `Dry run failed: ${d.error || (d.errors || []).join('; ') || 'unknown'}`
                  : `Dry run complete — ${num(d.checked || 0)} checked, `
                    + `${num((d.bison?.stopped || 0) + (d.heyreach?.stopped || 0))} would be stopped. No changes made.`)
              : 'Dry run complete — no changes made.')
          } else {
            const lr = s.rules?.[0]?.last_run
            const sum = lr?.summary
            setMsg(lr?.ok === false
              ? `Unenrollment check finished with an error: ${typeof sum === 'string' ? sum : (sum?.errors?.length ? sum.errors.join('; ') : (sum?.error || 'unknown'))}`
              : 'Check complete.')
          }
          setBusy(false)
          return
        }
      } catch { /* transient — keep polling */ }
    }
    setMsg('Unenrollment check is still running — refresh the page later.')
    setBusy(false)
  }

  if (!unenroll) return null
  return (
    <>
      <h2 className="section-h" style={{ marginTop: 18 }}>{heading}</h2>
      <p className="muted" style={{ fontSize: 12.5, marginTop: -4, maxWidth: 700 }}>
        Runs automatically every {unenroll.interval_minutes} minutes
        {unenroll.enabled === false ? ' (sweeper currently disabled)' : ''}. How the gate
        works is documented under Setup.
      </p>
      {(unenroll.rules || []).map((r) => (
        <RuleCard key={r.id} rule={r} busy={busy} msg={msg} onRun={runCheck}
          running={!!unenroll.running} progress={unenroll.progress} />
      ))}
    </>
  )
}
