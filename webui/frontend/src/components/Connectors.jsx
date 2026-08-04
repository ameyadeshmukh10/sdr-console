import { useState } from 'react'
import { BRAND } from '../theme.js'
import { api } from '../api.js'
import { Spinner } from './ui.jsx'

// Setup → Connectors. Two tiers, kept visually distinct on purpose:
//   * integrated  — code exists, status is measured from config
//   * available   — catalogue only, nothing is wired to it
// Blurring those would imply a provider is one toggle away when it isn't.

// Monogram tiles stand in for logos: the CSP on this app forbids remote assets, and
// shipping redrawn third-party marks would be both inaccurate and a trademark
// problem. Colour + monogram reads cleanly at this size. To use official artwork,
// drop an SVG at src/assets/connectors/<id>.svg and render it here instead.
const MONOGRAM = {
  hubspot: 'H', clay: 'C', prospeo: 'P', technographics: 'T', emailbison: 'B',
  heyreach: 'HR', anthropic: 'A', mongodb: 'M', salesforce: 'SF', dynamics: 'D',
  pipedrive: 'PD', zoominfo: 'ZI', apollo: 'AP', pitchbook: 'PB', crunchbase: 'CB',
  cognism: 'CG', clearbit: 'CL', salesnav: 'in', similarweb: 'SW', outreach: 'O',
  salesloft: 'SL', instantly: 'I', slack: 'S', snowflake: 'SN',
}

const STATUS = {
  connected: { label: 'Connected', color: BRAND.jade, mark: '✓' },
  built_in: { label: 'Built in', color: BRAND.jade, mark: '✓' },
  expired: { label: 'Reconnect needed', color: BRAND.red, mark: '!' },
  not_configured: { label: 'Not configured', color: BRAND.muted, mark: '' },
  available: { label: 'Available', color: BRAND.muted, mark: '' },
}

function ConnectorMark({ id, color, dim }) {
  return (
    <span className="conn-mark" style={{
      background: dim ? 'transparent' : color,
      border: dim ? `1.5px solid ${BRAND.border}` : `1.5px solid ${color}`,
      color: dim ? BRAND.muted : '#fff',
    }}>
      {MONOGRAM[id] || id.slice(0, 2).toUpperCase()}
    </span>
  )
}

// Wiring a connector up, in place. Two things make this safe to expose:
// the server never returns a stored secret (only a mask + which source it came
// from), and saving immediately runs a real READ against the provider, so a key
// that doesn't work says so here rather than three screens later during a send.
function ConnectorForm({ c, onDone }) {
  const [values, setValues] = useState({})
  const [busy, setBusy] = useState(null)
  const [result, setResult] = useState(null)
  const fields = c.fields || []

  async function save() {
    setBusy('save'); setResult(null)
    try {
      const r = await api.saveConnector(c.id, values)
      setResult(r.test || { ok: true, detail: 'Saved.' })
      setValues({})
      onDone()
    } catch (e) { setResult({ ok: false, detail: e.message }) }
    finally { setBusy(null) }
  }
  async function test() {
    setBusy('test'); setResult(null)
    try { setResult(await api.testConnector(c.id)) }
    catch (e) { setResult({ ok: false, detail: e.message }) }
    finally { setBusy(null) }
  }
  async function disconnect() {
    setBusy('off'); setResult(null)
    try { await api.disconnectConnector(c.id); setResult(null); onDone() }
    catch (e) { setResult({ ok: false, detail: e.message }) }
    finally { setBusy(null) }
  }

  const dirty = Object.values(values).some((v) => v !== undefined && v !== '')
  return (
    <div className="conn-form">
      {fields.map((f) => (
        <label key={f.key} className="field conn-field">
          <span>
            {f.label}{f.required && <span className="req"> *</span>}
            {f.set && (
              <span className="conn-src">
                {f.source === 'console' ? 'set here' : 'from environment'}
                {f.overrides_env && ' · overrides the deploy variable'}
              </span>
            )}
          </span>
          <input
            type={f.secret ? 'password' : 'text'}
            value={values[f.key] ?? (f.secret ? '' : f.value || '')}
            placeholder={f.secret && f.set ? f.value : ''}
            autoComplete="off" spellCheck="false"
            onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
          {f.help && <span className="conn-help">{f.help}</span>}
        </label>
      ))}
      <div className="card-actions" style={{ marginTop: 4 }}>
        <button className="primary sm" disabled={!!busy || !dirty} onClick={save}>
          {busy === 'save' ? <Spinner /> : 'Save & test'}
        </button>
        <button className="ghost sm" disabled={!!busy} onClick={test}>
          {busy === 'test' ? <Spinner /> : 'Test connection'}
        </button>
        {fields.some((f) => f.source === 'console') && (
          <button className="ghost sm" disabled={!!busy} onClick={disconnect}>
            {busy === 'off' ? <Spinner /> : 'Disconnect'}
          </button>
        )}
      </div>
      {result && (
        <div className={'banner ' + (result.ok ? 'info' : 'warn')}
          style={{ marginTop: 10, marginBottom: 0 }}>
          {result.ok ? '✓ ' : '⚠ '}{result.detail}
        </div>
      )}
      <p className="conn-help" style={{ marginTop: 8 }}>
        Stored on the persistent volume and applied immediately — no redeploy. Values
        are never shown again once saved.
      </p>
    </div>
  )
}

function ConnectorCard({ c, writable }) {
  const s = STATUS[c.status] || STATUS.available
  const live = c.status === 'connected' || c.status === 'built_in'
  const [open, setOpen] = useState(false)
  const canEdit = writable && c.integrated && c.configurable
  return (
    <div className={'conn-card' + (live ? ' live' : '') + (c.integrated ? '' : ' catalogue')
      + (open ? ' open' : '')} title={c.reason || undefined}>
      <div className="conn-head">
        <ConnectorMark id={c.id} color={c.color} dim={!live} />
        <div className="conn-body">
          <div className="conn-name-row">
            <span className="conn-name">{c.name}</span>
            {live && <span className="conn-check" aria-label="connected">✓</span>}
          </div>
          <div className="conn-blurb">{c.blurb}</div>
          <div className="conn-status" style={{ color: s.color }}>
            {c.status === 'expired' && '⚠ '}{s.label}
            {c.reason && c.integrated && c.status !== 'connected' && (
              <span className="conn-reason"> · {c.reason}</span>
            )}
          </div>
        </div>
        {canEdit && (
          <button className="ghost sm conn-edit" onClick={() => setOpen((v) => !v)}>
            {open ? 'Close' : live ? 'Manage' : 'Connect'}
          </button>
        )}
        {/* An OAuth or in-process connector has no key to type. Saying where it IS
            done beats rendering an empty form. */}
        {writable && c.integrated && !c.configurable && c.connect_via === 'oauth' && (
          <span className="conn-note">Authorize from the Use view</span>
        )}
      </div>
      {open && canEdit && <ConnectorForm c={c} onDone={() => setOpen(false)} />}
    </div>
  )
}

// `bare` renders the contents without the surrounding .panel — used when this sits
// inside the Setup tab panel, which already provides one.
export default function Connectors({ data, error, bare = false }) {
  const [showAvailable, setShowAvailable] = useState(false)
  const Wrap = bare
    ? ({ children }) => <div>{children}</div>
    : ({ children }) => <div className="panel">{children}</div>
  if (error) {
    return (
      <Wrap>
        <p className="muted" style={{ fontSize: 12.5 }}>Could not load connector state: {error}</p>
      </Wrap>
    )
  }
  if (!data) {
    return <Wrap><p className="muted" style={{ fontSize: 12.5 }}>Loading…</p></Wrap>
  }

  const all = data.connectors || []
  const integrated = all.filter((c) => c.integrated)
  const available = all.filter((c) => !c.integrated)
  const cats = data.categories || []
  const sum = data.summary || {}

  return (
    <Wrap>
      <div className="row between" style={{ alignItems: 'flex-start', gap: 12 }}>
        <div>
          <p className="muted" style={{ fontSize: 12.5, margin: '0 0 4px', maxWidth: 640 }}>
            What this AI SDR is wired into today, and what it could be. Statuses are read
            from configuration, so this page can't drift from reality.
          </p>
        </div>
        <span className="badge" style={{ color: BRAND.jade, borderColor: BRAND.jade, whiteSpace: 'nowrap' }}>
          {sum.connected} of {sum.integrated} connected
        </span>
      </div>

      {sum.needs_attention?.length > 0 && (
        <div className="banner warn" style={{ margin: '10px 0 0' }}>
          <b>Reconnect needed:</b> {sum.needs_attention.join(', ')} — the stored
          authorization can no longer be refreshed.
        </div>
      )}

      {cats.map((cat) => {
        const rows = integrated.filter((c) => c.category === cat.id)
        if (!rows.length) return null
        return (
          <div key={cat.id} style={{ marginTop: 16 }}>
            <div className="conn-cat">{cat.label}</div>
            <div className="conn-grid">
              {rows.map((c) => <ConnectorCard key={c.id} c={c} writable={data.writable} />)}
            </div>
          </div>
        )
      })}

      <div style={{ marginTop: 20, borderTop: `1px solid ${BRAND.border}`, paddingTop: 14 }}>
        <div className="row between" style={{ alignItems: 'center', gap: 12 }}>
          <div>
            <div className="conn-cat" style={{ marginBottom: 2 }}>
              Not connected — {sum.available} providers this could extend to
            </div>
            <p className="muted" style={{ fontSize: 12, margin: 0, maxWidth: 620 }}>
              Catalogue only. These are not integrations yet — nothing is wired to them,
              and turning one on means building it, not flipping a switch.
            </p>
          </div>
          <button className="ghost sm" onClick={() => setShowAvailable((v) => !v)}>
            {showAvailable ? 'Hide' : 'Show all'}
          </button>
        </div>
        {showAvailable && (
          <>
            {cats.map((cat) => {
              const rows = available.filter((c) => c.category === cat.id)
              if (!rows.length) return null
              return (
                <div key={cat.id} style={{ marginTop: 14 }}>
                  <div className="conn-cat sub">{cat.label}</div>
                  <div className="conn-grid">
                    {rows.map((c) => <ConnectorCard key={c.id} c={c} writable={data.writable} />)}
                  </div>
                </div>
              )
            })}
          </>
        )}
      </div>
    </Wrap>
  )
}
