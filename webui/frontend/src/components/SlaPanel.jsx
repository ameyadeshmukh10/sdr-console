import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'

// SLAs — the second way to feed the AI SDR (next to picking a list by hand): rules
// that check HubSpot on a schedule and enroll whoever matches. Each SLA maintains
// its own HubSpot static list ("AI SDR SLA · <name>"); a run adds the new matches
// and runs the standard pull + init on it, so ICP gating and suppression apply
// exactly as for a hand-picked list.
//
// Wizard: 1 Schedule (5|15 min · 1|5|10 h · 3|7|14 days at 22:00 UTC) → 2 Criteria
// (a HubSpot form whose new submissions trigger the run, and/or up to 10 account +
// 10 contact property filters with operator + value; at least one criterion) →
// 3 Confirm (name < 100 chars). The same wizard edits an existing SLA.

const EVERY = { minutes: [5, 15], hours: [1, 5, 10], days: [3, 7, 14] }
const OPS_BY_TYPE = {
  enumeration: ['IN', 'NOT_IN', 'EQ', 'NEQ', 'HAS_PROPERTY', 'NOT_HAS_PROPERTY'],
  bool: ['EQ', 'NEQ', 'HAS_PROPERTY', 'NOT_HAS_PROPERTY'],
  number: ['EQ', 'NEQ', 'GT', 'GTE', 'LT', 'LTE', 'BETWEEN', 'HAS_PROPERTY', 'NOT_HAS_PROPERTY'],
  date: ['GT', 'GTE', 'LT', 'LTE', 'BETWEEN', 'EQ', 'HAS_PROPERTY', 'NOT_HAS_PROPERTY'],
  datetime: ['GT', 'GTE', 'LT', 'LTE', 'BETWEEN', 'EQ', 'HAS_PROPERTY', 'NOT_HAS_PROPERTY'],
  string: ['EQ', 'NEQ', 'CONTAINS_TOKEN', 'NOT_CONTAINS_TOKEN', 'IN', 'NOT_IN', 'HAS_PROPERTY', 'NOT_HAS_PROPERTY'],
}
const OP_LABEL = {
  EQ: 'is', NEQ: 'is not', CONTAINS_TOKEN: 'contains', NOT_CONTAINS_TOKEN: "doesn't contain",
  HAS_PROPERTY: 'is known', NOT_HAS_PROPERTY: 'is unknown', IN: 'is any of', NOT_IN: 'is none of',
  GT: 'after / greater than', GTE: 'on or after / ≥', LT: 'before / less than', LTE: 'on or before / ≤', BETWEEN: 'between',
}
const NO_VALUE = ['HAS_PROPERTY', 'NOT_HAS_PROPERTY']
const LIST_OPS = ['IN', 'NOT_IN']
const fmtWhen = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—')

// ---- small pickers -----------------------------------------------------------
function FormPicker({ value, onChange }) {
  const [q, setQ] = useState('')
  const [forms, setForms] = useState(null)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState(null)
  const t = useRef(null)
  useEffect(() => {
    clearTimeout(t.current)
    t.current = setTimeout(async () => {
      try { const r = await api.hubspotForms(q); if (r.ok === false) setError(r.error); else { setForms(r.forms || []); setError(null) } }
      catch (e) { setError(e.message) }
    }, 250)
    return () => clearTimeout(t.current)
  }, [q])
  return (
    <div>
      {value ? (
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          <span className="badge cta">{value.name || value.id}</span>
          <span className="mono muted" style={{ fontSize: 11 }}>{value.id}</span>
          <button className="linklike" style={{ fontSize: 12 }} onClick={() => onChange(null)}>remove</button>
        </div>
      ) : (
        <div style={{ position: 'relative' }}>
          <input placeholder="Search HubSpot forms by name…" value={q} onFocus={() => setOpen(true)}
            onChange={(e) => { setQ(e.target.value); setOpen(true) }} style={{ width: '100%' }} />
          {error && <div className="banner error" style={{ marginTop: 6 }}>{error}</div>}
          {open && forms && (
            <div className="panel" style={{ position: 'absolute', zIndex: 5, left: 0, right: 0, marginTop: 4, padding: 0, maxHeight: 220, overflow: 'auto' }}>
              {forms.length === 0 && <div className="empty" style={{ padding: 10 }}>No forms match.</div>}
              {forms.slice(0, 60).map((f) => (
                <div key={f.id} className="inbox-row" style={{ padding: '7px 12px', cursor: 'pointer', fontSize: 13 }}
                  onClick={() => { onChange({ id: f.id, name: f.name }); setOpen(false) }}>
                  {f.name} <span className="muted" style={{ fontSize: 11 }}>· {f.type}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function PropertyPicker({ object, onPick, onCancel }) {
  const [q, setQ] = useState('')
  const [props, setProps] = useState(null)
  const [error, setError] = useState(null)
  const t = useRef(null)
  useEffect(() => {
    clearTimeout(t.current)
    t.current = setTimeout(async () => {
      try { const r = await api.hubspotProperties(object, q); if (r.ok === false) setError(r.error); else { setProps(r.properties || []); setError(null) } }
      catch (e) { setError(e.message) }
    }, 250)
    return () => clearTimeout(t.current)
  }, [q, object])
  return (
    <div className="panel" style={{ padding: 10, marginTop: 8 }}>
      <div className="row" style={{ gap: 8, alignItems: 'center' }}>
        <input autoFocus placeholder={`Search ${object === 'companies' ? 'account' : 'contact'} properties…`} value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1 }} />
        <button className="ghost sm" onClick={onCancel}>Cancel</button>
      </div>
      {error && <div className="banner error" style={{ marginTop: 6 }}>{error}</div>}
      {!props && !error && <div style={{ marginTop: 8 }}><Spinner label="Loading properties…" /></div>}
      {props && (
        <div style={{ maxHeight: 220, overflow: 'auto', marginTop: 8 }}>
          {props.length === 0 && <div className="empty" style={{ padding: 8 }}>No properties match.</div>}
          {props.slice(0, 80).map((p) => (
            <div key={p.name} className="inbox-row" style={{ padding: '6px 10px', cursor: 'pointer', fontSize: 13 }} onClick={() => onPick(p)}>
              {p.label} <span className="mono muted" style={{ fontSize: 11 }}>{p.name}</span> <span className="muted" style={{ fontSize: 11 }}>· {p.type}{p.group ? ` · ${p.group}` : ''}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function opsFor(type) { return OPS_BY_TYPE[type] || OPS_BY_TYPE.string }

function FilterRow({ f, onChange, onRemove }) {
  const ops = opsFor(f.type)
  const isEnum = f.type === 'enumeration' && (f.options || []).length > 0
  const isDate = f.type === 'date' || f.type === 'datetime'
  const inputType = isDate ? 'date' : (f.type === 'number' ? 'number' : 'text')
  const noValue = NO_VALUE.includes(f.operator)
  const listOp = LIST_OPS.includes(f.operator)
  return (
    <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap', padding: '6px 0', borderBottom: '1px solid var(--hairline)' }}>
      <span style={{ minWidth: 170, fontSize: 13 }} title={f.property}><b>{f.label || f.property}</b> <span className="muted" style={{ fontSize: 11 }}>{f.type}</span></span>
      <select value={f.operator} onChange={(e) => onChange({ ...f, operator: e.target.value })} style={{ fontSize: 12.5, padding: '4px 8px' }}>
        {ops.map((o) => <option key={o} value={o}>{OP_LABEL[o] || o}</option>)}
      </select>
      {!noValue && listOp && isEnum && (
        <select multiple value={f.values || []} onChange={(e) => onChange({ ...f, values: [...e.target.selectedOptions].map((o) => o.value) })}
          style={{ fontSize: 12.5, minWidth: 180, height: Math.min(6, (f.options || []).length) * 22 + 8 }} title="Cmd/Ctrl-click for several">
          {(f.options || []).map((o) => <option key={o.value} value={o.value}>{o.label || o.value}</option>)}
        </select>
      )}
      {!noValue && listOp && !isEnum && (
        // Keep the raw text the user types (so commas survive re-renders) and derive
        // the list from it; values_text is UI-only and stripped before saving.
        <input placeholder="value1, value2, …"
          value={f.values_text !== undefined ? f.values_text : (f.values || []).join(', ')}
          onChange={(e) => onChange({ ...f, values_text: e.target.value, values: e.target.value.split(',').map((v) => v.trim()).filter(Boolean) })}
          onBlur={() => onChange({ ...f, values_text: (f.values || []).join(', ') })}
          style={{ minWidth: 220 }} />
      )}
      {!noValue && !listOp && isEnum && (
        <select value={f.value || ''} onChange={(e) => onChange({ ...f, value: e.target.value })} style={{ fontSize: 12.5, padding: '4px 8px' }}>
          <option value="">— pick —</option>
          {(f.options || []).map((o) => <option key={o.value} value={o.value}>{o.label || o.value}</option>)}
        </select>
      )}
      {!noValue && !listOp && !isEnum && f.type === 'bool' && (
        <select value={f.value || ''} onChange={(e) => onChange({ ...f, value: e.target.value })} style={{ fontSize: 12.5, padding: '4px 8px' }}>
          <option value="">— pick —</option><option value="true">true</option><option value="false">false</option>
        </select>
      )}
      {!noValue && !listOp && !isEnum && f.type !== 'bool' && (
        <>
          <input type={inputType} value={f.value || ''} onChange={(e) => onChange({ ...f, value: e.target.value })} placeholder="value" style={{ width: isDate ? 150 : 180 }} />
          {f.operator === 'BETWEEN' && <><span className="muted">and</span><input type={inputType} value={f.high_value || ''} onChange={(e) => onChange({ ...f, high_value: e.target.value })} placeholder="high" style={{ width: isDate ? 150 : 180 }} /></>}
        </>
      )}
      <button className="linklike" style={{ fontSize: 12, marginLeft: 'auto' }} onClick={onRemove}>remove</button>
    </div>
  )
}

function FilterGroup({ title, object, filters, onChange, max }) {
  const [picking, setPicking] = useState(false)
  return (
    <div className="panel" style={{ padding: 12, marginTop: 10 }}>
      <div className="row between" style={{ alignItems: 'center' }}>
        <b style={{ fontSize: 13 }}>{title} <span className="muted" style={{ fontWeight: 400 }}>· {filters.length}/{max}</span></b>
        <button className="ghost sm" disabled={picking || filters.length >= max} onClick={() => setPicking(true)}>+ Add property</button>
      </div>
      {filters.length === 0 && !picking && <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>No {title.toLowerCase()} yet — optional.</div>}
      {filters.map((f, i) => (
        <FilterRow key={`${f.property}-${i}`} f={f}
          onChange={(nf) => onChange(filters.map((x, j) => (j === i ? nf : x)))}
          onRemove={() => onChange(filters.filter((_, j) => j !== i))} />
      ))}
      {picking && (
        <PropertyPicker object={object} onCancel={() => setPicking(false)}
          onPick={(p) => {
            const type = p.type || 'string'
            const defaultOp = opsFor(type)[0]
            onChange([...filters, { property: p.name, label: p.label, type, field_type: p.field_type, options: p.options || [], operator: defaultOp, value: '', values: [] }])
            setPicking(false)
          }} />
      )}
    </div>
  )
}

// ---- wizard -------------------------------------------------------------------
function SlaWizard({ initial, onClose, onSaved }) {
  const [step, setStep] = useState(1)
  const [sla, setSla] = useState(() => ({
    id: initial?.id || null, name: initial?.name || '',
    schedule: initial?.schedule || { every: 15, unit: 'minutes' },
    form: initial?.form || null,
    company_filters: initial?.company_filters || [],
    contact_filters: initial?.contact_filters || [],
    enabled: initial?.enabled !== false,
  }))
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  // Editing: enumeration options are UI-only (not stored) — fetch them back so the
  // value pickers render as selects again.
  useEffect(() => {
    if (!initial) return
    ;(async () => {
      for (const [key, object] of [['company_filters', 'companies'], ['contact_filters', 'contacts']]) {
        const need = (initial[key] || []).filter((f) => f.type === 'enumeration' && !(f.options || []).length)
        for (const f of need) {
          try {
            const r = await api.hubspotProperties(object, f.property)
            const p = (r.properties || []).find((x) => x.name === f.property)
            if (p?.options?.length) setSla((s) => ({ ...s, [key]: s[key].map((x) => (x.property === f.property ? { ...x, options: p.options } : x)) }))
          } catch { /* keep the text input */ }
        }
      }
    })()
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  const nCriteria = (sla.form ? 1 : 0) + sla.company_filters.length + sla.contact_filters.length
  const canFinish = nCriteria > 0 && sla.name.trim().length > 0 && sla.name.trim().length < 100

  function setUnit(unit) { setSla((s) => ({ ...s, schedule: { unit, every: EVERY[unit][0] } })) }
  async function save() {
    setSaving(true); setError(null)
    try {
      const r = await api.saveSla({
        ...sla,
        company_filters: sla.company_filters.map(({ options, values_text, ...f }) => f),   // options / values_text are UI-only
        contact_filters: sla.contact_filters.map(({ options, values_text, ...f }) => f),
      })
      onSaved(r.sla)
    } catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }
  const scheduleText = sla.schedule.unit === 'days'
    ? `every ${sla.schedule.every} days at 22:00 UTC` : `every ${sla.schedule.every} ${sla.schedule.unit}`

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer" style={{ maxWidth: 760 }}>
        <button className="close-x" onClick={onClose}>Close ✕</button>
        <h2 className="page-title" style={{ marginBottom: 2 }}>{sla.id ? 'Edit SLA' : 'Create SLA'}</h2>
        <p className="page-sub" style={{ marginBottom: 12 }}>
          An SLA checks HubSpot on a schedule and enrolls whoever matches — through the same pull and ICP gates as a hand-picked list.
        </p>
        <div className="tabs" style={{ marginBottom: 12 }}>
          {['1 · Schedule', '2 · Criteria', '3 · Confirm'].map((t, i) => (
            <button key={t} className={`tab${step === i + 1 ? ' active' : ''}`} onClick={() => setStep(i + 1)}>{t}</button>
          ))}
        </div>
        <ErrorBanner error={error} />

        {step === 1 && (
          <div>
            <div className="section-h" style={{ marginTop: 0 }}>How often should the system check the CRM for enrollment?</div>
            <div className="row" style={{ gap: 14, alignItems: 'flex-end', flexWrap: 'wrap' }}>
              <label className="field">Every
                <select value={sla.schedule.every} onChange={(e) => setSla((s) => ({ ...s, schedule: { ...s.schedule, every: Number(e.target.value) } }))}>
                  {EVERY[sla.schedule.unit].map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </label>
              <label className="field">Unit
                <select value={sla.schedule.unit} onChange={(e) => setUnit(e.target.value)}>
                  <option value="minutes">minutes</option><option value="hours">hours</option><option value="days">days</option>
                </select>
              </label>
              <span className="muted" style={{ fontSize: 12.5, paddingBottom: 8 }}>
                → {scheduleText}{sla.schedule.unit === 'days' ? ' (day schedules always fire at 22:00 UTC)' : ''}
              </span>
            </div>
            <div className="row" style={{ justifyContent: 'flex-end', marginTop: 18 }}>
              <button onClick={() => setStep(2)}>Next: criteria →</button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div>
            <div className="section-h" style={{ marginTop: 0 }}>What should be enrolled? <span className="muted" style={{ fontWeight: 400, textTransform: 'none', letterSpacing: 0 }}>everything is optional — but pick at least one</span></div>
            <div className="panel" style={{ padding: 12 }}>
              <b style={{ fontSize: 13 }}>Form</b>
              <div className="muted" style={{ fontSize: 12.5, margin: '2px 0 8px' }}>When a new submission is received it invokes the whole thing — submitters are the candidates; the property criteria below then filter them.</div>
              <FormPicker value={sla.form} onChange={(f) => setSla((s) => ({ ...s, form: f }))} />
            </div>
            <FilterGroup title="Account properties" object="companies" filters={sla.company_filters} max={10}
              onChange={(fl) => setSla((s) => ({ ...s, company_filters: fl }))} />
            <FilterGroup title="Contact properties" object="contacts" filters={sla.contact_filters} max={10}
              onChange={(fl) => setSla((s) => ({ ...s, contact_filters: fl }))} />
            <div className="row between" style={{ marginTop: 18 }}>
              <button className="ghost" onClick={() => setStep(1)}>← Back</button>
              <span className="row" style={{ gap: 10, alignItems: 'center' }}>
                {nCriteria === 0 && <span className="muted" style={{ fontSize: 12.5 }}>pick a form or add at least one property to continue</span>}
                <button disabled={nCriteria === 0} onClick={() => setStep(3)}>Next: confirm →</button>
              </span>
            </div>
          </div>
        )}

        {step === 3 && (
          <div>
            <div className="section-h" style={{ marginTop: 0 }}>Name it and confirm</div>
            <label className="field" style={{ maxWidth: 480 }}>SLA name <span className="muted">(under 100 characters)</span>
              <input value={sla.name} maxLength={99} autoFocus placeholder="e.g. Inbound RFP form → AI SDR" onChange={(e) => setSla((s) => ({ ...s, name: e.target.value }))} />
            </label>
            <div className="kv" style={{ marginTop: 14 }}>
              <span className="k">Schedule</span><span>{scheduleText}</span>
              <span className="k">Form</span><span>{sla.form ? `${sla.form.name} (${sla.form.id})` : <span className="muted">—</span>}</span>
              <span className="k">Account</span><span>{sla.company_filters.length ? sla.company_filters.map((f) => `${f.label || f.property} ${OP_LABEL[f.operator]} ${NO_VALUE.includes(f.operator) ? '' : (LIST_OPS.includes(f.operator) ? (f.values || []).join(' | ') : (f.operator === 'BETWEEN' ? `${f.value} – ${f.high_value}` : f.value))}`).join(' · ') : <span className="muted">—</span>}</span>
              <span className="k">Contact</span><span>{sla.contact_filters.length ? sla.contact_filters.map((f) => `${f.label || f.property} ${OP_LABEL[f.operator]} ${NO_VALUE.includes(f.operator) ? '' : (LIST_OPS.includes(f.operator) ? (f.values || []).join(' | ') : (f.operator === 'BETWEEN' ? `${f.value} – ${f.high_value}` : f.value))}`).join(' · ') : <span className="muted">—</span>}</span>
              <span className="k">HubSpot list</span><span className="muted">“AI SDR SLA · {sla.name || '…'}” — created on the first run; new matches are added there, then pulled + batched like any list.</span>
            </div>
            <label className="row" style={{ gap: 8, alignItems: 'center', cursor: 'pointer', marginTop: 8, fontSize: 13 }}>
              <input type="checkbox" checked={sla.enabled} onChange={(e) => setSla((s) => ({ ...s, enabled: e.target.checked }))} style={{ width: 'auto' }} />
              active (unticked = saved but paused)
            </label>
            <div className="row between" style={{ marginTop: 18 }}>
              <button className="ghost" onClick={() => setStep(2)}>← Back</button>
              <button disabled={!canFinish || saving} onClick={save}>{saving ? <Spinner label="Saving…" /> : (sla.id ? 'Save changes' : 'Create SLA')}</button>
            </div>
          </div>
        )}
      </div>
    </>
  )
}

// ---- panel ----------------------------------------------------------------------
const NO_VALUE_SET = new Set(NO_VALUE)
function filterText(f) {
  const v = NO_VALUE_SET.has(f.operator) ? '' : (LIST_OPS.includes(f.operator) ? (f.values || []).join(' | ') : (f.operator === 'BETWEEN' ? `${f.value} – ${f.high_value}` : f.value))
  return `${f.label || f.property} ${OP_LABEL[f.operator] || f.operator} ${v}`.trim()
}

// Full detail for one SLA — opens under its row on click, so nothing has to be
// crammed into the row itself.
function SlaDetail({ s, busy, onRun, onEdit, onToggle, onDelete }) {
  const lr = s.last_run
  return (
    <div className="panel" style={{ margin: '4px 0 10px', padding: 16, background: 'var(--panel-2)' }}>
      <div className="row between" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div>
          <div className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <b style={{ fontSize: 15 }}>{s.name}</b>
            <span className={`badge ${s.enabled ? 'status-enrolled' : 'status-pending'}`}>{s.enabled ? 'active' : 'paused'}</span>
            {s.running && <span className="row" style={{ gap: 5, fontSize: 12 }}><span className="spinner" />running</span>}
          </div>
          <div className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>{s.schedule_label} · created {fmtWhen(s.created_at)}{s.created_by ? ` by ${s.created_by}` : ''}{s.updated_at && s.updated_at !== s.created_at ? ` · edited ${fmtWhen(s.updated_at)}` : ''}</div>
        </div>
        <span className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          <button className="sm" disabled={busy || s.running} onClick={onRun}>Run now</button>
          <button className="ghost sm" disabled={busy} onClick={onEdit}>Edit</button>
          <button className="ghost sm" disabled={busy} onClick={onToggle}>{s.enabled ? 'Pause' : 'Resume'}</button>
          <button className="ghost sm" disabled={busy} style={{ color: 'var(--red)' }} onClick={onDelete}>Delete</button>
        </span>
      </div>

      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 14, marginTop: 14 }}>
        <div>
          <div className="section-h" style={{ margin: '0 0 6px' }}>Criteria</div>
          {s.form && <div style={{ fontSize: 13 }}>Form: <b>{s.form.name || s.form.id}</b> <span className="muted" style={{ fontSize: 11 }}>· new submissions trigger the run</span></div>}
          {(s.company_filters || []).length > 0 && (
            <div style={{ fontSize: 13, marginTop: 4 }}><span className="muted">Account:</span>
              <ul style={{ margin: '2px 0 0 18px' }}>{s.company_filters.map((f, i) => <li key={i}>{filterText(f)}</li>)}</ul>
            </div>
          )}
          {(s.contact_filters || []).length > 0 && (
            <div style={{ fontSize: 13, marginTop: 4 }}><span className="muted">Contact:</span>
              <ul style={{ margin: '2px 0 0 18px' }}>{s.contact_filters.map((f, i) => <li key={i}>{filterText(f)}</li>)}</ul>
            </div>
          )}
        </div>
        <div>
          <div className="section-h" style={{ margin: '0 0 6px' }}>Runs</div>
          <div className="kv" style={{ margin: 0, gridTemplateColumns: '90px 1fr' }}>
            <span className="k">Last run</span>
            <span>{lr ? <>{fmtWhen(lr.at)} · {lr.ok === false ? <span style={{ color: 'var(--red)' }}>failed</span> : 'ok'} · {num(lr.matched || 0)} matched · {num(lr.added || 0)} added{lr.pull?.new_contacts != null ? ` · ${num(lr.pull.new_contacts)} enrolled` : ''}</> : <span className="muted">never</span>}</span>
            <span className="k">Next run</span><span>{s.enabled ? fmtWhen(s.next_run_at) : <span className="muted">paused</span>}</span>
            <span className="k">Totals</span><span>{num(s.totals?.runs || 0)} runs · {num(s.totals?.matched || 0)} matched · {num(s.totals?.added || 0)} added</span>
            <span className="k">HubSpot list</span><span>{s.hubspot_list_id ? <><span className="mono">{s.hubspot_list_id}</span> <span className="muted">{s.hubspot_list_name || ''}</span></> : <span className="muted">created on the first run</span>}</span>
          </div>
        </div>
      </div>

      {(s.runs || []).length > 0 && (
        <div style={{ marginTop: 12, overflowX: 'auto' }}>
          <table className="dense" style={{ margin: 0 }}>
            <thead><tr><th>When</th><th>Mode</th><th>Matched</th><th>New</th><th>Added</th><th>Pull</th><th>Error</th></tr></thead>
            <tbody>
              {(s.runs || []).slice(0, 10).map((r, i) => (
                <tr key={i}>
                  <td style={{ whiteSpace: 'nowrap' }}>{fmtWhen(r.at)}</td><td>{r.mode}{r.dry_run ? ' (dry)' : ''}</td>
                  <td>{num(r.matched || 0)}{r.form_submissions ? <span className="muted"> · {num(r.form_submissions)} subs</span> : null}</td>
                  <td>{num(r.new || 0)}</td><td>{num(r.added || 0)}</td>
                  <td className="muted">{r.pull ? (r.pull.ok ? `+${num(r.pull.new_contacts || 0)} contacts · +${num(r.pull.new_batches || 0)} batches` : `failed (${r.pull.stage})`) : '—'}</td>
                  <td style={{ color: 'var(--red)', maxWidth: 260 }}>{r.error || ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function SlaPanel({ onChanged }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [wizard, setWizard] = useState(null)   // null | {} (new) | sla (edit)
  const [busy, setBusy] = useState(null)
  const [openId, setOpenId] = useState(null)   // sla whose detail card is open
  const timer = useRef(null)

  async function load() {
    try { const r = await api.slas(); setData(r); setError(null) } catch (e) { setError(e.message) }
  }
  useEffect(() => { load() }, [])
  useEffect(() => {
    const running = (data?.slas || []).some((s) => s.running)
    clearInterval(timer.current)
    if (running) timer.current = setInterval(async () => { await load() }, 3000)
    return () => clearInterval(timer.current)
  }, [data])

  async function act(id, fn) {
    setBusy(id); setError(null)
    try { await fn(); await load(); onChanged && onChanged() } catch (e) { setError(e.message) }
    finally { setBusy(null) }
  }
  const slas = data?.slas || []

  return (
    <div className="panel" style={{ marginBottom: 22 }}>
      <div className="row between" style={{ alignItems: 'center', gap: 12 }}>
        <div className="section-h" style={{ margin: 0 }}>Schedule SLAs</div>
        <button onClick={() => setWizard({})}>+ Create SLA</button>
      </div>
      {data && data.enabled_loop === false && <div className="muted" style={{ fontSize: 12, marginTop: 6, color: 'var(--amber)' }}>Scheduler is off on this server (SLA_ENABLED=0) — SLAs only run via Run.</div>}
      <ErrorBanner error={error} />
      {!data && <div style={{ marginTop: 10 }}><Spinner label="Loading SLAs…" /></div>}
      {data && slas.length === 0 && <div className="muted" style={{ fontSize: 12.5, marginTop: 10 }}>No SLAs yet.</div>}
      {slas.length > 0 && (
        <div className="panel" style={{ padding: 0, marginTop: 12 }}>
          <table className="dense">
            <thead><tr><th>SLA</th><th>Schedule</th><th style={{ width: 150 }}></th></tr></thead>
            <tbody>
              {slas.map((s) => {
                const isOpen = openId === s.id
                return [
                  <tr key={s.id} onClick={() => setOpenId(isOpen ? null : s.id)}
                    style={{ cursor: 'pointer', opacity: s.enabled ? 1 : 0.65, background: isOpen ? 'var(--surface-hover)' : undefined }}
                    title={isOpen ? 'click to close' : 'click for details'}>
                    <td>
                      <b>{s.name}</b>
                      {!s.enabled && <span className="badge status-pending" style={{ marginLeft: 8, fontSize: 10 }}>paused</span>}
                      {s.running && <span className="row" style={{ gap: 5, fontSize: 12, marginLeft: 8, display: 'inline-flex' }}><span className="spinner" />running</span>}
                    </td>
                    <td style={{ whiteSpace: 'nowrap' }}>{s.schedule_label}</td>
                    <td style={{ whiteSpace: 'nowrap' }} onClick={(e) => e.stopPropagation()}>
                      <button className="sm" disabled={busy === s.id || s.running} onClick={() => act(s.id, () => api.runSla(s.id))} title="Evaluate now and enroll matches">Run</button>{' '}
                      <button className="ghost sm" disabled={busy === s.id} onClick={() => setWizard(s)}>Edit</button>
                    </td>
                  </tr>,
                  isOpen && (
                    <tr key={s.id + '-detail'}>
                      <td colSpan={3} style={{ padding: '0 10px' }}>
                        <SlaDetail s={s} busy={busy === s.id}
                          onRun={() => act(s.id, () => api.runSla(s.id))}
                          onEdit={() => setWizard(s)}
                          onToggle={() => act(s.id, () => api.toggleSla(s.id, !s.enabled))}
                          onDelete={() => { if (window.confirm(`Delete SLA “${s.name}”? Its HubSpot list stays; nothing already enrolled changes.`)) { setOpenId(null); act(s.id, () => api.deleteSla(s.id)) } }} />
                      </td>
                    </tr>
                  ),
                ]
              })}
            </tbody>
          </table>
        </div>
      )}
      {wizard && (
        <SlaWizard initial={wizard.id ? wizard : null} onClose={() => setWizard(null)}
          onSaved={async () => { setWizard(null); await load(); onChanged && onChanged() }} />
      )}
    </div>
  )
}
