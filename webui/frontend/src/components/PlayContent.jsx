import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner } from './ui.jsx'

// What content this play carries, and how to add or remove it.
//
// An offer is a promise ("I'll show you a signal play"); the content is the
// evidence behind it. Before this the evidence lived only in the knowledge base as
// prose, so there was no way to see which proof a given touch was leaning on, and
// no way to swap it — you edited a markdown file and hoped.
//
// Most proof already LIVES somewhere — a case-study page, a deck, a shared doc — so
// a piece of content is usually just a LINK plus enough summary for the writer to
// use it. The link is shown, and it opens.
//
// The one field that is not cosmetic is `nameable`. Naming a customer who has not
// agreed is a real problem, and it is a fact about the customer rather than about
// the play, so it lives on the content and is rendered here in plain language. When
// it is off, the customer's name is never sent to the copy generator at all.

const KINDS = [
  { id: 'proof', label: 'Customer proof' },
  { id: 'asset', label: 'Asset (deck, one-pager)' },
  { id: 'doc', label: 'Reference doc' },
]

function host(url) {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return 'link' }
}

export function ContentChip({ item, onRemove, busy }) {
  return (
    <span className={'content-chip' + (item.nameable ? '' : ' anon')}>
      <span className="content-chip-k">{item.kind === 'proof' ? '“' : '¶'}</span>
      <span>
        {item.nameable ? item.customer : (item.anonymous || 'unnamed customer')}
        {item.metric && <span className="content-metric"> · {item.metric}</span>}
      </span>
      {item.url && (
        <a href={item.url} target="_blank" rel="noreferrer" className="content-link"
          title={item.url} onClick={(e) => e.stopPropagation()}>
          {host(item.url)} ↗
        </a>
      )}
      {!item.nameable && <span className="content-anon" title={
        'This customer has not agreed to be named. The copy generator is given '
        + `"${item.anonymous || item.industry || 'an existing customer'}" and never `
        + 'the name.'}>unnamed</span>}
      {onRemove && (
        <button type="button" className="x" disabled={busy}
          title="Remove from this play" onClick={onRemove}>×</button>
      )}
    </span>
  )
}

// The per-step block inside the sequence editor.
export default function PlayContent({ ctaKey, content, onChanged }) {
  const [lib, setLib] = useState(null)
  const [adding, setAdding] = useState(false)
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (adding && !lib) api.references().then(setLib).catch((e) => setError(e.message))
  }, [adding])

  async function attach(refKey, detach) {
    setBusy(true); setError(null)
    try {
      await api.attachContent({ cta_key: ctaKey, reference_key: refKey, detach })
      setAdding(false); onChanged?.()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const repos = lib?.repositories
  const items = content || []
  const attached = new Set(items.map((i) => i.ref_key))
  const available = (lib?.references || []).filter((r) => !attached.has(r.ref_key))

  if (!ctaKey) return null

  return (
    <div className="play-content">
      <div className="play-content-h">
        <span>Content this play carries</span>
        <button type="button" className="ghost sm" disabled={busy}
          onClick={() => setAdding((v) => !v)}>
          {adding ? 'Cancel' : '+ Add content'}
        </button>
      </div>

      <ErrorBanner error={error} />

      {items.length === 0 ? (
        <div className="play-content-empty">
          Nothing attached — the copy leans on the knowledge base alone. Add a case
          study or an asset and the writer may cite it.
        </div>
      ) : (
        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          {items.map((i) => (
            <ContentChip key={i.ref_key} item={i} busy={busy}
              onRemove={() => attach(i.ref_key, true)} />
          ))}
        </div>
      )}

      {adding && (
        <div className="play-picker">
          {!lib ? <Spinner label="Loading the library…" /> : (
            <>
              {available.length > 0 ? available.map((r) => (
                <button key={r.ref_key} type="button" className="play-pick"
                  disabled={busy} onClick={() => attach(r.ref_key, false)}>
                  <b>{r.nameable ? r.customer : (r.anonymous || 'unnamed customer')}</b>
                  {r.metric ? ` — ${r.metric}` : ''}
                  <span className="play-pick-story">{r.story}</span>
                </button>
              )) : (
                <div className="muted" style={{ fontSize: 12 }}>
                  Everything in the library is already on this play.
                </div>
              )}
              <button type="button" className="ghost sm" style={{ marginTop: 8 }}
                onClick={() => setCreating(true)}>
                New content…
              </button>
              {/* Where content can come from. The distinction that matters here is
                  narrower than "is it configured": a LINK works from anywhere, and
                  connecting a repository only changes whether the console can browse
                  it for you. Saying that plainly stops the empty library reading as
                  "nothing is set up". */}
              {repos && (
                <div className="repo-strip">
                  <span className="repo-strip-h">Content sources</span>
                  {repos.repositories.map((r) => (
                    <span key={r.id}
                      className={'repo-chip' + (r.browsable ? ' on' : '')}
                      title={r.browsable
                        ? `${r.name} is connected — ${r.blurb}`
                        : `${r.name} isn't connected. You can still link to it.`}>
                      {r.name}{r.browsable ? ' ✓' : ''}
                    </span>
                  ))}
                  <span className="repo-note">{repos.note}</span>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {creating && (
        <NewContent onClose={() => setCreating(false)}
          onSaved={async (key) => { setCreating(false); setLib(null); await attach(key, false) }} />
      )}
    </div>
  )
}

function NewContent({ onClose, onSaved }) {
  const [f, setF] = useState({ ref_key: '', customer: '', story: '', metric: '',
    url: '', industry: '', anonymous: '', quote: '', source: '', kind: 'proof',
    nameable: false })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  async function save() {
    setBusy(true); setError(null)
    try {
      const key = f.ref_key || f.customer.toLowerCase().replace(/[^a-z0-9]+/g, '-')
      await api.saveReference({ ...f, ref_key: key })
      onSaved(key)
    } catch (e) { setError(e.message); setBusy(false) }
  }

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer" style={{ width: 520, overflowY: 'auto' }}>
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
          <h2 style={{ margin: 0 }}>New content</h2>
          <button className="ghost sm" onClick={onClose}>Close</button>
        </div>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          A proof point or an asset a play can cite. If it already lives somewhere —
          a case study, a deck — link it rather than retyping it.
        </p>
        <ErrorBanner error={error} />

        <label className="field">Customer
          <input value={f.customer} onChange={set('customer')} placeholder="Memgraph" />
        </label>
        <label className="field" style={{ marginTop: 10 }}>Link to the source
          <input value={f.url} onChange={set('url')}
            placeholder="https://… case study, deck, doc" />
        </label>
        <label className="field" style={{ marginTop: 10 }}>What happened
          <textarea rows={3} value={f.story} onChange={set('story')}
            placeholder="One or two sentences the writer can work from." />
        </label>
        <div className="row" style={{ gap: 10, marginTop: 10, flexWrap: 'wrap' }}>
          <label className="field" style={{ flex: 1, minWidth: 190 }}>The number
            <input value={f.metric} onChange={set('metric')}
              placeholder="$2.7M pipeline in 90 days" />
          </label>
          <label className="field" style={{ width: 160 }}>Kind
            <select value={f.kind} onChange={set('kind')}>
              {KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
            </select>
          </label>
        </div>

        {/* The consequential switch. Off by default — assuming permission to name a
            customer is the wrong default to have. */}
        <div style={{ marginTop: 16, paddingTop: 14, borderTop: '1px solid var(--border)' }}>
          <label className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
            <input type="checkbox" checked={f.nameable} style={{ width: 'auto', marginTop: 2 }}
              onChange={(e) => setF({ ...f, nameable: e.target.checked })} />
            <span>
              <span style={{ fontSize: 13, fontWeight: 600 }}>
                We may name this customer in outreach
              </span>
              <span className="muted" style={{ fontSize: 11, display: 'block' }}>
                Only tick this if they have agreed. When it is off the name is never
                sent to the copy generator — it is given the description below instead.
              </span>
            </span>
          </label>
          {!f.nameable && (
            <div className="row" style={{ gap: 10, marginTop: 10, flexWrap: 'wrap' }}>
              <label className="field" style={{ flex: 1, minWidth: 200 }}>
                Describe them instead
                <input value={f.anonymous} onChange={set('anonymous')}
                  placeholder="a mid-market logistics platform" />
              </label>
              <label className="field" style={{ width: 160 }}>Industry
                <input value={f.industry} onChange={set('industry')} placeholder="Logistics" />
              </label>
            </div>
          )}
          {f.nameable && (
            <label className="field" style={{ marginTop: 10 }}>Usable quote (optional)
              <textarea rows={2} value={f.quote} onChange={set('quote')} />
            </label>
          )}
        </div>

        <label className="field" style={{ marginTop: 12 }}>Where it came from
          <input value={f.source} onChange={set('source')}
            placeholder="Customer deck p12 / QBR call / public case study" />
        </label>

        <div className="row" style={{ gap: 10, marginTop: 18 }}>
          <button className="primary" disabled={busy || !f.customer || !f.story}
            onClick={save}>{busy ? <Spinner /> : 'Save & attach'}</button>
          <button className="ghost" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </>
  )
}
