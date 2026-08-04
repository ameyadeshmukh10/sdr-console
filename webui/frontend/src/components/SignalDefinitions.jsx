import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'

// What counts as a signal here — configurable, because it is the most
// customer-specific thing in the product.
//
// The shipped kinds are the ones the pipeline can go and detect: account news,
// hiring, tech stack. But the strongest buying trigger a team has is usually
// already in their CRM and specific to them — "opened fourteen emails and never
// replied", "we lost a deal to them in Q1". Those can't be constants, so a signal
// kind can carry a RULE and the console evaluates it.
//
// Two things this screen has to get right:
//   * STRENGTH is editable, because it is the only number that says how much a
//     signal is worth relative to the others, and that ordering is a judgement
//     about this business, not a fact.
//   * A rule shows what it MATCHES before it can be saved. "Times contacted is at
//     least 5" is abstract; "27 accounts, here are three of them" is a decision.

const DECAYS = [
  { v: 0.3, label: 'Very slowly', hint: 'a warm history stays relevant for months' },
  { v: 0.5, label: 'Slowly', hint: 'still worth mentioning next quarter' },
  { v: 1, label: 'Normally', hint: 'the default — meaningful for a few weeks' },
  { v: 2, label: 'Quickly', hint: 'stale within a couple of weeks' },
  { v: 3, label: 'Very quickly', hint: 'intent — a page view is not news three weeks on' },
]

const DETECTOR_LABEL = {
  scan: 'Detected by a scan', llm: 'Researched at copy time',
  crm: 'Arrives from the CRM', internal: 'Computed from our own history',
  rule: 'Derived by a rule',
}

function strengthWord(n) {
  if (n >= 44) return 'a conversation on its own'
  if (n >= 34) return 'a strong reason to call'
  if (n >= 22) return 'worth mentioning'
  return 'background colour'
}

export default function SignalDefinitions() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(null)   // kind, or '__new__'
  const [busy, setBusy] = useState(null)

  function load() {
    api.signalDefs().then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
  }
  useEffect(() => { load() }, [])

  async function patch(kind, fields) {
    setBusy(kind); setError(null)
    try { await api.saveSignalDef({ kind, ...fields }); load() }
    catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  async function run(kind) {
    setBusy(kind); setError(null)
    try {
      const r = await api.runSignalDef(kind, {})
      setError(r.error ? `${kind}: ${r.error}` : null)
      load()
    } catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  async function remove(kind) {
    setBusy(kind); setError(null)
    try { await api.deleteSignalDef(kind); load() }
    catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  if (!data) return <div className="panel"><Spinner label="Loading signal definitions…" /></div>
  if (!data.available) {
    return (
      <div className="panel">
        <div className="empty">Signal definitions aren’t available for this dataset.</div>
      </div>
    )
  }

  const rows = data.signals || []
  const derived = rows.filter((r) => r.rule)

  return (
    <div className="panel" style={{ marginTop: 22 }}>
      <div className="card-h">
        <div>
          <h3>What counts as a signal</h3>
          <div className="card-note">
            Every kind the console recognises, and how much each is worth. Campaigns
            qualify on these, the priority score weighs them, and the Signals feed
            groups by them.
          </div>
        </div>
        <button className="primary sm" onClick={() => setEditing('__new__')}>
          New signal
        </button>
      </div>

      <ErrorBanner error={error} />

      <div className="panel" style={{ padding: 0, marginTop: 12, overflowX: 'auto' }}>
        <table className="dense" style={{ width: '100%', minWidth: 860 }}>
          <thead><tr>
            <th style={{ width: '20%' }}>Signal</th>
            <th style={{ width: '26%' }}>When it fires</th>
            <th style={{ width: '16%' }}>Worth</th>
            <th style={{ width: '14%' }}>Goes stale</th>
            <th style={{ width: '10%' }}>Observed</th>
            <th style={{ width: '14%' }} />
          </tr></thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.kind} className={s.active ? '' : 'row-off'}>
                <td>
                  <div style={{ fontWeight: 600 }}>{s.label}</div>
                  <div className="muted mono" style={{ fontSize: 11 }}>{s.kind}</div>
                </td>
                <td className="muted" style={{ fontSize: 12 }}>
                  {s.rule_text || DETECTOR_LABEL[s.detector] || '—'}
                  {s.description && !s.rule_text && (
                    <div className="clamp2" style={{ fontSize: 11 }}>{s.description}</div>
                  )}
                  {s.last_run_detail && (
                    <div style={{ fontSize: 11, color: 'var(--jade)' }}>
                      last run: {num(s.last_run_detail.accounts)} accounts
                    </div>
                  )}
                </td>
                <td>
                  <div className="row" style={{ gap: 8, alignItems: 'center' }}>
                    <input type="range" min="0" max="50" step="1" value={s.strength}
                      disabled={busy === s.kind} className="strength"
                      onChange={(e) => patch(s.kind, { strength: Number(e.target.value) })} />
                    <b style={{ fontVariantNumeric: 'tabular-nums' }}>{Math.round(s.strength)}</b>
                  </div>
                  <div className="muted" style={{ fontSize: 10.5 }}>
                    {strengthWord(s.strength)}
                  </div>
                </td>
                <td>
                  <select value={String(s.decay_scale ?? 1)} disabled={busy === s.kind}
                    style={{ width: '100%', fontSize: 12 }}
                    onChange={(e) => patch(s.kind, { decay_scale: Number(e.target.value) })}>
                    {DECAYS.map((d) => (
                      <option key={d.v} value={String(d.v)} title={d.hint}>{d.label}</option>
                    ))}
                  </select>
                </td>
                <td className="muted">{num(s.events)}</td>
                <td>
                  <div className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                    {s.rule && (
                      <>
                        <button className="ghost sm" disabled={busy === s.kind}
                          onClick={() => run(s.kind)} title="Evaluate now and record matches">
                          {busy === s.kind ? <Spinner /> : 'Run'}
                        </button>
                        <button className="ghost sm" onClick={() => setEditing(s.kind)}>
                          Edit
                        </button>
                      </>
                    )}
                    {/* Builtins deactivate but never delete: stored events reference
                        the id, and deleting one would strand its history. */}
                    <button className="ghost sm" disabled={busy === s.kind}
                      title={s.active ? 'Stop offering and stop running this'
                        : 'Offer this again'}
                      onClick={() => patch(s.kind, { active: !s.active })}>
                      {s.active ? 'Turn off' : 'Turn on'}
                    </button>
                    {!s.builtin && (
                      <button className="ghost sm" disabled={busy === s.kind}
                        onClick={() => remove(s.kind)}>Delete</button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="hint" style={{ marginTop: 10 }}>
        {derived.length > 0
          ? <>{derived.length} of these are derived from your CRM and re-evaluate on the
            hourly sweep. Turning one off stops it running.</>
          : <>None of these are CRM-derived yet. <b>New signal</b> builds one from a
            field you already have — prior activity, a past deal, a lifecycle stage.</>}
      </div>

      {editing && (
        <RuleEditor
          vocab={data.vocabulary}
          crmAvailable={data.crm_available}
          existing={editing === '__new__' ? null : rows.find((r) => r.kind === editing)}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); load() }}
        />
      )}
    </div>
  )
}

// Build the rule, and see what it catches before saving it.
function RuleEditor({ vocab, existing, crmAvailable, onClose, onSaved }) {
  const sources = vocab?.sources || []
  const operators = vocab?.operators || []
  const templates = vocab?.templates || []

  const [kind, setKind] = useState(existing?.kind || '')
  const [label, setLabel] = useState(existing?.label || '')
  const [strength, setStrength] = useState(existing?.strength ?? 35)
  const [decay, setDecay] = useState(String(existing?.decay_scale ?? 1))
  const [rule, setRule] = useState(existing?.rule
    || { source: 'local_field', field: 'lifecycle_stage', op: 'eq', value: '' })
  const [preview, setPreview] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const src = sources.find((s) => s.id === rule.source)
  const fields = src?.fields || []
  const field = fields.find((f) => f.key === rule.field)
  const op = operators.find((o) => o.id === rule.op)
  const needsValue = op && !op.valueless
  const blocked = src?.needs_crm && !crmAvailable

  function applyTemplate(t) {
    setKind(existing ? kind : t.kind)
    setLabel(t.signal_label)
    setStrength(t.strength)
    setDecay(String(t.decay_scale))
    setRule(t.rule)
    setPreview(null)
  }

  function setSource(id) {
    const s = sources.find((x) => x.id === id)
    const first = s?.fields?.[0]
    setRule({ source: id, field: first?.key, op: id === 'deal' ? 'exists' : 'eq',
      ...(id === 'deal' ? { window_days: 365 } : {}) })
    setPreview(null)
  }

  async function runPreview() {
    setBusy('preview'); setError(null)
    try { setPreview(await api.previewSignalRule({ kind: kind || 'preview', label, rule })) }
    catch (e) { setError(e.message); setPreview(null) } finally { setBusy(null) }
  }

  async function save() {
    setBusy('save'); setError(null)
    try {
      await api.saveSignalDef({
        kind, label, strength: Number(strength),
        decay_scale: Number(decay), rule,
      })
      onSaved()
    } catch (e) { setError(e.message); setBusy(null) }
  }

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer" style={{ width: 560, overflowY: 'auto' }}>
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
          <h2 style={{ margin: 0 }}>{existing ? 'Edit signal' : 'New signal'}</h2>
          <button className="ghost sm" onClick={onClose}>Close</button>
        </div>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          A rule over data you already have. When it matches, the account gets this
          signal — and every campaign that qualifies on it picks the account up.
        </p>

        <ErrorBanner error={error} />

        {!existing && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
              Start from one of these
            </div>
            <div className="brief-examples">
              {templates.map((t) => (
                <button key={t.id} type="button" onClick={() => applyTemplate(t)}>
                  <b>{t.label}</b> — {t.hint}
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="row" style={{ gap: 10, flexWrap: 'wrap', marginBottom: 14 }}>
          <label className="field" style={{ flex: 1, minWidth: 200 }}>
            Name it
            <input value={label} onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. Prior activity" />
          </label>
          <label className="field" style={{ width: 190 }}>
            Id
            <input value={kind} disabled={!!existing} className="mono"
              placeholder="prior_activity"
              onChange={(e) => setKind(e.target.value.toLowerCase()
                .replace(/[^a-z0-9_]/g, '_'))} />
          </label>
        </div>

        <div style={{ paddingTop: 14, borderTop: '1px solid var(--border)' }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>It fires when…</div>
          <div className="opt-grid" style={{ marginBottom: 12 }}>
            {sources.map((s) => (
              <button key={s.id} type="button"
                className={'opt' + (rule.source === s.id ? ' on' : '')}
                onClick={() => setSource(s.id)}>
                <span className="opt-t">{s.label}</span>
                <span className="opt-d">{s.note}</span>
              </button>
            ))}
          </div>

          <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <label className="field" style={{ minWidth: 190, flex: 1 }}>
              {rule.source === 'deal' ? 'Which deals' : 'Field'}
              <select value={rule.field || ''}
                onChange={(e) => { setRule({ ...rule, field: e.target.value }); setPreview(null) }}>
                {fields.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
              </select>
            </label>
            {rule.source !== 'deal' && (
              <label className="field" style={{ width: 150 }}>
                Test
                <select value={rule.op}
                  onChange={(e) => { setRule({ ...rule, op: e.target.value }); setPreview(null) }}>
                  {operators
                    .filter((o) => field?.type !== 'number' || o.numeric || o.valueless)
                    .map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                </select>
              </label>
            )}
            {needsValue && rule.source !== 'deal' && (
              <label className="field" style={{ width: 140 }}>
                Value
                <input type={field?.type === 'number' ? 'number' : 'text'}
                  value={rule.value ?? ''}
                  onChange={(e) => { setRule({ ...rule, value: e.target.value }); setPreview(null) }} />
              </label>
            )}
            {rule.source === 'deal' && (
              <label className="field" style={{ width: 150 }}>
                Within
                <select value={String(rule.window_days || 365)}
                  onChange={(e) => { setRule({ ...rule, window_days: Number(e.target.value) }); setPreview(null) }}>
                  {[90, 180, 365, 730].map((d) => (
                    <option key={d} value={d}>{d} days</option>
                  ))}
                </select>
              </label>
            )}
          </div>

          {blocked && (
            <div className="banner warn" style={{ marginTop: 12, marginBottom: 0 }}>
              This source reads from the CRM, which isn’t connected — you can save the
              rule, but it can’t be previewed or run until HubSpot is wired up in Setup.
            </div>
          )}

          <div className="card-actions">
            <button className="ghost sm" disabled={!!busy || blocked} onClick={runPreview}>
              {busy === 'preview' ? <Spinner /> : 'What does this match?'}
            </button>
          </div>

          {/* The count is the point. A rule is abstract until you can see whether it
              catches three accounts or nine hundred. */}
          {preview && (
            <div className={'banner ' + (preview.error ? 'warn' : 'info')}
              style={{ marginTop: 4, marginBottom: 0 }}>
              {preview.error ? preview.error : (
                <>
                  <b>{num(preview.accounts)} accounts</b> ({num(preview.matched)} contacts)
                  out of {num(preview.scanned)} checked.
                  {preview.accounts === 0 && ' Nothing matches — try loosening the test.'}
                  {(preview.sample || []).length > 0 && (
                    <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                      {preview.sample.slice(0, 4).map((s) => (
                        <div key={s.domain}>
                          {s.company || s.domain}
                          {s.who ? ` · ${s.who}` : ''}
                          {s.value != null && s.value !== '' ? ` · ${s.value}` : ''}
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>

        <div style={{ marginTop: 18, paddingTop: 14, borderTop: '1px solid var(--border)' }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>How much is it worth?</div>
          <div className="row" style={{ gap: 10, alignItems: 'center' }}>
            <input type="range" min="0" max="50" value={strength} className="strength"
              style={{ flex: 1 }} onChange={(e) => setStrength(Number(e.target.value))} />
            <b style={{ width: 28, textAlign: 'right' }}>{strength}</b>
          </div>
          <div className="muted" style={{ fontSize: 11 }}>
            {strengthWord(strength)} — for comparison, account news is 50 and a detected
            tech stack is 27.
          </div>
          <label className="field" style={{ marginTop: 10, width: 220 }}>
            Goes stale
            <select value={decay} onChange={(e) => setDecay(e.target.value)}>
              {DECAYS.map((d) => <option key={d.v} value={String(d.v)}>{d.label} — {d.hint}</option>)}
            </select>
          </label>
        </div>

        <div className="row" style={{ gap: 10, marginTop: 18 }}>
          <button className="primary" disabled={!!busy || !kind || !label} onClick={save}>
            {busy === 'save' ? <Spinner /> : existing ? 'Save changes' : 'Create signal'}
          </button>
          <button className="ghost" onClick={onClose}>Cancel</button>
        </div>
        <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          Saving only defines it. Press <b>Run</b> on the list to evaluate it now, or
          leave it to the hourly sweep.
        </div>
      </div>
    </>
  )
}
