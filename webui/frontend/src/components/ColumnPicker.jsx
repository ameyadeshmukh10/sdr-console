import { useEffect, useState } from 'react'
import { api } from '../api.js'

// Column configuration for the call list.
//
// The built-in columns are the ones the scoring model produces. Everything the CRM
// field map knows about is offered alongside them, so a property RevOps wires up
// becomes a column here without a code change — which is the point: the useful
// column is always the one specific to how a team works, and that can't be
// predicted from here.
//
// The choice is per-browser (localStorage). It's a view preference, not shared
// state, and persisting it server-side would mean one rep's layout changing
// everyone else's.

const KEY = 'sdr_calllist_columns'

// id must match a renderer in CallList's COLUMNS map. `crm:<local_key>` ids are
// resolved dynamically against the member's CRM values.
export const BUILT_IN = [
  { id: 'money', label: '$' },
  { id: 'score', label: 'Score', always: true },
  { id: 'momentum', label: 'Trend' },
  { id: 'name', label: 'Name', always: true },
  { id: 'company', label: 'Company' },
  { id: 'title', label: 'Title' },
  { id: 'phone', label: 'Phone', hint: 'Scale package' },
  { id: 'persona', label: 'Persona' },
  { id: 'buyer_role', label: 'Buyer role' },
  { id: 'channels', label: 'Channels' },
  { id: 'campaigns', label: 'Campaigns' },
  { id: 'state', label: 'State' },
  { id: 'signal', label: 'Why they’re here' },
  { id: 'signal_kind', label: 'Signal type' },
  { id: 'origin', label: 'Source' },
  { id: 'qualified_at', label: 'Qualified' },
  { id: 'email', label: 'Email' },
]

const DEFAULT = ['money', 'score', 'momentum', 'name', 'company', 'title', 'channels',
  'campaigns', 'state', 'signal']

export function loadColumns() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || 'null')
    if (Array.isArray(v) && v.length) return v
  } catch { /* ignore */ }
  return DEFAULT
}

export function saveColumns(ids) {
  localStorage.setItem(KEY, JSON.stringify(ids))
}

export default function ColumnPicker({ value, onChange }) {
  const [open, setOpen] = useState(false)
  const [crmFields, setCrmFields] = useState([])

  // Anything mapped to the CRM is offerable as a column — that is how a new field
  // gets into this list without touching code.
  useEffect(() => {
    api.crmFields()
      .then((d) => setCrmFields((d.fields || []).filter((f) => f.enabled)))
      .catch(() => {})
  }, [])

  const options = [
    ...BUILT_IN,
    ...crmFields.map((f) => ({
      id: `crm:${f.local_key}`,
      label: f.label || f.local_key,
      crm: true,
      hint: `${f.object_type} · ${f.property_name}`,
    })),
  ]

  function toggle(id) {
    const opt = options.find((o) => o.id === id)
    if (opt?.always) return
    onChange(value.includes(id) ? value.filter((x) => x !== id) : [...value, id])
  }

  return (
    <div style={{ position: 'relative' }}>
      <button type="button" className="ghost sm" onClick={() => setOpen(!open)}>
        Columns ({value.length})
      </button>
      {open && (
        <>
          <div className="drawer-backdrop" style={{ background: 'transparent', zIndex: 30 }}
            onClick={() => setOpen(false)} />
          <div className="panel col-pop">
            <div className="section-h" style={{ marginTop: 0 }}>Show columns</div>
            <div className="col-list">
              {options.map((o) => (
                <label key={o.id} className={'f-check' + (o.always ? ' muted' : '')}
                  title={o.hint}>
                  <input type="checkbox" checked={value.includes(o.id)}
                    disabled={o.always} onChange={() => toggle(o.id)} />
                  <span>
                    {o.label}
                    {o.crm && <span className="tag" style={{ marginLeft: 6 }}>CRM</span>}
                    {o.hint && <span className="hint">{o.hint}</span>}
                  </span>
                </label>
              ))}
            </div>
            <div className="card-actions">
              <button className="ghost sm" onClick={() => onChange(DEFAULT)}>Reset</button>
              <span className="hint">
                Any CRM field you wire up appears here automatically.
              </span>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
