import { useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner } from './ui.jsx'
import { BRAND } from '../theme.js'
import { useDemo } from '../DemoContext.jsx'

// Chat-driven editing for one Setup section. Describe a change (or attach a doc),
// get a real unified diff, approve or discard. The diff IS the review step — nothing
// is written from the conversation alone.

const TEXT_EXT = /\.(md|markdown|txt|csv|tsv|json|ya?ml)$/i
const MAX_FILE_BYTES = 200 * 1024

function DiffView({ diff }) {
  return (
    <pre className="diff">
      {diff.split('\n').map((line, i) => {
        let cls = 'ctx'
        if (line.startsWith('+++') || line.startsWith('---')) cls = 'meta'
        else if (line.startsWith('@@')) cls = 'hunk'
        else if (line.startsWith('+')) cls = 'add'
        else if (line.startsWith('-')) cls = 'del'
        return <span key={i} className={'dl ' + cls}>{line || ' '}</span>
      })}
    </pre>
  )
}

function ProposalCard({ prop, onApply, onDiscard, busy }) {
  const [openPath, setOpenPath] = useState(prop.changes?.[0]?.path || null)
  if (!prop.changes?.length) {
    return (
      <div className="cc-prop">
        <b>No change proposed.</b>
        {prop.notes && <p className="cc-notes">{prop.notes}</p>}
        {prop.warnings?.map((w, i) => <p key={i} className="cc-warn">⚠ {w}</p>)}
      </div>
    )
  }
  const active = prop.changes.find((c) => c.path === openPath) || prop.changes[0]
  return (
    <div className="cc-prop">
      <div className="row between" style={{ alignItems: 'flex-start', gap: 10 }}>
        <b>Proposed change</b>
        <span className="muted" style={{ fontSize: 11 }}>
          {prop.changes.length} file{prop.changes.length > 1 ? 's' : ''}
        </span>
      </div>
      {prop.notes && <p className="cc-notes">{prop.notes}</p>}
      {prop.warnings?.map((w, i) => <p key={i} className="cc-warn">⚠ {w}</p>)}

      <div className="cc-filetabs">
        {prop.changes.map((c) => (
          <button key={c.path} onClick={() => setOpenPath(c.path)}
            className={'cc-filetab' + (c.path === active.path ? ' active' : '')}>
            {c.path.split('/').pop()}
            <span className="cc-stat add">+{c.added}</span>
            <span className="cc-stat del">−{c.removed}</span>
          </button>
        ))}
      </div>
      {active.summary && <p className="cc-summary">{active.summary}</p>}
      <DiffView diff={active.diff} />

      <div className="row" style={{ gap: 8, marginTop: 10, alignItems: 'center' }}>
        <button className="sm" onClick={() => onApply(prop.id)} disabled={busy}>
          {busy ? <Spinner label="Applying…" /> : 'Approve & write'}
        </button>
        <button className="ghost sm" onClick={onDiscard} disabled={busy}>Discard</button>
        <span className="muted" style={{ fontSize: 11 }}>
          Writes the file on this server. Reversible from the history below.
        </span>
      </div>
    </div>
  )
}

export default function ConfigChat({ scope, meta, persistence, available, history, onApplied }) {
  const [turns, setTurns] = useState([])       // {role, text} | {role:'proposal', prop}
  const [input, setInput] = useState('')
  const [attachments, setAttachments] = useState([])
  const [busy, setBusy] = useState(false)
  const [applying, setApplying] = useState(false)
  const [err, setErr] = useState(null)
  const fileRef = useRef(null)
  const { profileId } = useDemo()

  // Hidden in demo mode: writes are blocked there, and a disabled box with a
  // read-only notice is exactly the kind of caveat a demo shouldn't show. Making
  // this demoable needs canned proposals, not a live model call.
  if (!meta || profileId) return null

  if (!meta.editable) {
    return (
      <div className="cc-locked">
        <b>Editing from chat is not enabled for this section.</b>
        <p className="muted" style={{ fontSize: 12, margin: '6px 0 0', lineHeight: 1.55 }}>
          This section controls {meta.affects} Change it in{' '}
          <span className="mono">{meta.paths.join(', ')}</span> and redeploy.
        </p>
      </div>
    )
  }

  async function pickFiles(e) {
    const files = [...(e.target.files || [])]
    e.target.value = ''
    const next = []
    for (const f of files) {
      if (!TEXT_EXT.test(f.name)) {
        setErr(`${f.name}: only text files (.md, .txt, .csv, .json, .yaml) can be read — `
          + 'export a PDF or doc to text first.')
        continue
      }
      if (f.size > MAX_FILE_BYTES) {
        setErr(`${f.name} is larger than 200 KB.`)
        continue
      }
      next.push({ name: f.name, text: await f.text() })
    }
    if (next.length) setAttachments((a) => [...a, ...next].slice(0, 5))
  }

  async function send() {
    const instruction = input.trim()
    if (!instruction && !attachments.length) return
    setErr(null)
    setBusy(true)
    const label = instruction || `Incorporate ${attachments.map((a) => a.name).join(', ')}`
    setTurns((t) => [...t, { role: 'user', text: label,
                             files: attachments.map((a) => a.name) }])
    setInput('')
    const sent = attachments
    setAttachments([])
    try {
      const prop = await api.configPropose({ scope, instruction, attachments: sent })
      setTurns((t) => [...t, { role: 'proposal', prop }])
    } catch (e) {
      setErr(e.status === 501
        ? `Not available: ${e.message}. Set ANTHROPIC_API_KEY on the server.`
        : e.message)
    } finally {
      setBusy(false)
    }
  }

  async function apply(proposalId) {
    setApplying(true)
    setErr(null)
    try {
      const res = await api.configApply({ proposal_id: proposalId })
      setTurns((t) => [
        ...t.filter((x) => !(x.role === 'proposal' && x.prop.id === proposalId)),
        { role: 'applied', text: `Written: ${res.applied.files.join(', ')}`, patch: res.patch },
      ])
      onApplied?.()
    } catch (e) {
      setErr(e.message)
    } finally {
      setApplying(false)
    }
  }

  function discard(id) {
    setTurns((t) => t.filter((x) => !(x.role === 'proposal' && x.prop.id === id)))
  }

  async function revert(entryId) {
    setErr(null)
    try {
      await api.configRevert({ entry_id: entryId })
      onApplied?.()
    } catch (e) { setErr(e.message) }
  }

  const disabled = !!profileId || !available

  return (
    <div className="cc">
      <div className="cc-head">
        <div>
          <b style={{ fontSize: 13.5 }}>Change this with chat</b>
          <p className="muted" style={{ fontSize: 12, margin: '3px 0 0', maxWidth: 620, lineHeight: 1.5 }}>
            Describe the change or attach a document. You get a diff to approve — nothing
            is written until you do.
          </p>
        </div>
      </div>

      {!available && (
        <p className="cc-warn">
          ANTHROPIC_API_KEY is not set on the server, so proposals can't be generated.
        </p>
      )}
      {persistence && !persistence.durable && (
        <p className="cc-note">{persistence.note}</p>
      )}

      {turns.length > 0 && (
        <div className="cc-thread">
          {turns.map((t, i) => {
            if (t.role === 'user') {
              return (
                <div key={i} className="cc-turn user">
                  {t.text}
                  {t.files?.length > 0 && (
                    <div className="cc-attached">{t.files.map((f) => `📎 ${f}`).join('  ')}</div>
                  )}
                </div>
              )
            }
            if (t.role === 'applied') {
              return (
                <div key={i} className="cc-turn applied">
                  ✓ {t.text}
                  <details style={{ marginTop: 6 }}>
                    <summary style={{ cursor: 'pointer', fontSize: 11.5 }}>
                      Patch to commit
                    </summary>
                    <DiffView diff={t.patch || ''} />
                  </details>
                </div>
              )
            }
            return (
              <ProposalCard key={i} prop={t.prop} busy={applying}
                onApply={apply} onDiscard={() => discard(t.prop.id)} />
            )
          })}
        </div>
      )}

      {attachments.length > 0 && (
        <div className="cc-pending">
          {attachments.map((a) => (
            <span key={a.name} className="cc-chip">
              📎 {a.name}
              <button onClick={() => setAttachments((x) => x.filter((y) => y.name !== a.name))}>×</button>
            </span>
          ))}
        </div>
      )}

      <div className="cc-input">
        <textarea rows={2} value={input} disabled={disabled || busy}
          placeholder={`e.g. "add the Memgraph result as a citable proof point" — edits ${meta.paths.map((p) => p.split('/').pop()).join(', ')}`}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send() }
          }} />
        <div className="cc-actions">
          <button className="ghost sm" onClick={() => fileRef.current?.click()} disabled={disabled || busy}>
            Attach
          </button>
          <button className="sm" onClick={send} disabled={disabled || busy || (!input.trim() && !attachments.length)}>
            {busy ? <Spinner label="Thinking…" /> : 'Propose change'}
          </button>
        </div>
        <input ref={fileRef} type="file" multiple hidden onChange={pickFiles}
          accept=".md,.markdown,.txt,.csv,.tsv,.json,.yaml,.yml" />
      </div>
      {err && <p className="cc-warn" style={{ marginTop: 8 }}>{err}</p>}

      {history?.length > 0 && (
        <div className="cc-history">
          <div className="muted" style={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.5px', marginBottom: 6 }}>
            Change history
          </div>
          {history.filter((h) => h.scope === scope).map((h) => (
            <div key={h.id + (h.reverted ? '-r' : '')} className="cc-hrow">
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 12 }}>
                  {h.instruction || h.attachments?.join(', ') || '(no instruction)'}
                </div>
                <div className="muted" style={{ fontSize: 10.5 }}>
                  {h.applied_at} · {h.files?.join(', ')}
                  {h.actor ? ` · ${h.actor}` : ''}
                  {h.reverted ? ' · reverted' : ''}
                </div>
              </div>
              {!h.reverted && (
                <button className="ghost sm" onClick={() => revert(h.id)} disabled={disabled}>
                  Revert
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
