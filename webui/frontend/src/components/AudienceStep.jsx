import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import ContactUpload from './ContactUpload.jsx'

// Step 1 — WHO is in scope.
//
// A campaign has two independent filters and keeping them separate is what makes
// the workflow legible:
//     audience      which accounts/contacts are in the pool at all
//     signal_query  which of those are worth working right now
// "Everyone we lost a deal to last month" is an audience. "Raised funding in the
// last 14 days" is a signal. You usually want both.
//
// The preview is the point of this step: an audience is abstract until you can see
// how many contacts it reaches — and, just as important, how many the CRM knows
// about that we have never pulled.

export default function AudienceStep({ campaign, onSaved }) {
  const [vocab, setVocab] = useState(null)
  const [aud, setAud] = useState(campaign.audience || { type: 'all_contacts' })
  const [preview, setPreview] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [lists, setLists] = useState([])
  const [imports, setImports] = useState([])

  useEffect(() => { api.audienceVocab().then(setVocab).catch((e) => setError(e.message)) }, [])
  useEffect(() => {
    if (aud.type === 'upload') api.contactImports().then((d) => setImports(d.imports || [])).catch(() => {})
  }, [aud.type])
  useEffect(() => {
    if (aud.type === 'hubspot_list' && lists.length === 0) {
      api.hubspotLists('', '0-1').then((d) => setLists(d.lists || [])).catch(() => {})
    }
  }, [aud.type])

  async function runPreview(a) {
    setBusy('preview'); setError(null)
    try { setPreview(await api.previewAudience(a || aud)) }
    catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  async function save() {
    setBusy('save'); setError(null)
    try { await api.updateCampaign(campaign.campaign_id, { audience: aud }); onSaved() }
    catch (e) { setError(e.message) } finally { setBusy(null) }
  }

  const preset = (vocab?.presets || []).find((p) => p.id === aud.preset)
  const dirty = JSON.stringify(aud) !== JSON.stringify(campaign.audience || { type: 'all_contacts' })

  return (
    <div className="panel">
      <div className="card-h"><h3>Who is in scope</h3></div>
      <p className="card-note">
        The pool this campaign draws from. Narrow here by <i>who they are</i>; the next step
        narrows by <i>what just happened</i>.
      </p>

      <ErrorBanner error={error} />

      <div className="opt-grid" style={{ margin: '16px 0 14px' }}>
        <Choice on={aud.type === 'all_contacts'} onClick={() => setAud({ type: 'all_contacts' })}
          label="Everyone" hint="All contacts already pulled into the pipeline" />
        <Choice on={aud.type === 'hubspot_list'}
          onClick={() => setAud({ type: 'hubspot_list', list_id: '' })}
          label="A HubSpot list" hint="Static or dynamic — membership resolves in HubSpot" />
        <Choice on={aud.type === 'crm_query'}
          onClick={() => setAud({ type: 'crm_query', preset: 'closed_lost', days: 30 })}
          label="A CRM segment" hint="Computed live from deals and lifecycle" />
        {/* The list that exists only as a file. Neither of the other two can reach
            it, and it is usually the freshest audience anyone has. */}
        <Choice on={aud.type === 'upload'}
          onClick={() => setAud({ type: 'upload', import_id: null })}
          label="A file I drop in" hint="Event list or CSV — imported to the CRM too" />
      </div>

      {aud.type === 'upload' && (
        <div style={{ marginBottom: 12 }}>
          {imports.length > 0 && (
            <label className="field" style={{ marginBottom: 10 }}>
              Use a list you already imported
              <select value={aud.import_id || ''} style={{ minWidth: 320 }}
                onChange={(e) => {
                  const im = imports.find((x) => String(x.import_id) === e.target.value)
                  setAud({ type: 'upload', import_id: Number(e.target.value) || null,
                    label: im?.label })
                }}>
                <option value="">— import a new file below —</option>
                {imports.map((im) => (
                  <option key={im.import_id} value={im.import_id}>
                    {im.label} ({num(im.contacts)} contacts)
                  </option>
                ))}
              </select>
            </label>
          )}
          <ContactUpload onImported={(r) => { setAud(r.audience); runPreview(r.audience) }} />
        </div>
      )}

      {aud.type === 'hubspot_list' && (
        <div style={{ marginBottom: 12 }}>
          <select value={aud.list_id || ''} style={{ minWidth: 320 }}
            onChange={(e) => {
              const l = lists.find((x) => String(x.list_id) === e.target.value)
              setAud({ type: 'hubspot_list', list_id: e.target.value, list_name: l?.name })
            }}>
            <option value="">— pick a contact list —</option>
            {lists.map((l) => (
              <option key={l.list_id} value={l.list_id}>{l.name} ({num(l.size)})</option>
            ))}
          </select>
          {lists.length === 0 && (
            <span className="hint" style={{ marginLeft: 8 }}>Loading lists from HubSpot…</span>
          )}
        </div>
      )}

      {aud.type === 'crm_query' && (
        <div style={{ marginBottom: 12 }}>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
            {(vocab?.presets || []).map((p) => (
              <button key={p.id} type="button"
                className={aud.preset === p.id ? 'primary sm' : 'ghost sm'}
                onClick={() => setAud({
                  type: 'crm_query', preset: p.id,
                  ...(p.default_days ? { days: p.default_days } : {}),
                  ...(p.id === 'lifecycle' ? { lifecycle_stage: 'marketingqualifiedlead' } : {}),
                })}>{p.label}</button>
            ))}
          </div>
          {preset?.description && <p className="card-note">{preset.description}</p>}
          {preset?.default_days != null && (
            <label className="f">
              Window{' '}
              <input type="number" min="1" max="3650" style={{ width: 80 }}
                value={aud.days ?? preset.default_days}
                onChange={(e) => setAud({ ...aud, days: Number(e.target.value) })} /> days
            </label>
          )}
          {aud.preset === 'lifecycle' && (
            <label className="f">
              Stage{' '}
              <input value={aud.lifecycle_stage || ''} style={{ width: 220 }}
                onChange={(e) => setAud({ ...aud, lifecycle_stage: e.target.value })}
                placeholder="marketingqualifiedlead" />
            </label>
          )}
        </div>
      )}

      <div className="card-actions">
        <button className="ghost sm" disabled={busy} onClick={() => runPreview()}>
          {busy === 'preview' ? <Spinner /> : 'Preview reach'}
        </button>
        <button className="primary sm" disabled={busy || !dirty} onClick={save}>
          {busy === 'save' ? <Spinner /> : 'Save audience'}
        </button>
        {dirty && <span className="hint">Unsaved change</span>}
      </div>

      {preview && (
        <div className="banner info" style={{ marginTop: 14, marginBottom: 0 }}>
          <b>{preview.description}</b> — {num(preview.contacts)} contacts across{' '}
          {num((preview.domains || []).length)} accounts we hold.
          {preview.stats?.error && (
            <div style={{ color: 'var(--red)', marginTop: 4 }}>
              Could not resolve: {preview.stats.error}
            </div>
          )}
          {preview.stats?.not_in_pipeline > 0 && (
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {num(preview.stats.not_in_pipeline)} more match in the CRM but have never been
              pulled into the pipeline. Pull them from the Source tab first — adding contacts
              changes the pool, so it stays a separate step.
            </div>
          )}
          {preview.stats?.deals != null && (
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              From {num(preview.stats.deals)} matching deals.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Choice({ on, onClick, label, hint }) {
  return (
    <button type="button" onClick={onClick} className={'opt' + (on ? ' on' : '')}>
      <span className="opt-t">{label}</span>
      <span className="opt-d">{hint}</span>
    </button>
  )
}
