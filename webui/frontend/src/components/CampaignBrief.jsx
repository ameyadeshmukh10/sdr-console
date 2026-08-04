import { useRef, useState } from 'react'
import { api } from '../api.js'
import { Spinner, ErrorBanner } from './ui.jsx'

// Configure the campaign by describing it, or by dropping the spec.
//
// The form below this box asks eleven structured questions. Every one of them is a
// translation of a decision someone already made in a sentence — "go back at
// everyone we lost this quarter, lead with the hiring angle, only accounts actually
// building a sales team". This does the translation, so the meeting note is the
// input and the filled form is the output.
//
// It PROPOSES, it never submits. Fields visibly change, the user reads them and
// still presses Create — which is what keeps a model driving a targeting screen
// safe, and also what makes it useful: you can see exactly what it understood.
//
// Clarifying questions are the other half. A spec that doesn't settle the window,
// or which accounts count, comes back as a question with concrete options rather
// than a confident default; picking one applies its own overlay, so the answer
// visibly moves the rest of the form.

const EXAMPLES = [
  'Re-engage everyone we lost a deal to in the last quarter, lead with whatever changed at the account since.',
  'Target companies hiring sales reps right now — at least 3 open sales roles — and pitch covering pipeline while the new reps ramp.',
  'Go after accounts running Outreach or Salesloft. The angle is that we sit on top of their existing infrastructure, not replace it.',
]

// What can be dropped here. Transcripts are the point as much as specs are: the
// artifact a campaign decision actually leaves behind is a meeting, and .vtt/.srt
// exports and Word documents are how those arrive. .docx is binary, so everything
// goes up base64 and the server does one decode (see campaign_brief.read_attachment),
// rather than the client guessing at encodings.
const TEXT_EXT = /\.(txt|md|markdown|csv|json|ya?ml|rtf|log|vtt|srt)$/i
const BINARY_EXT = /\.(docx)$/i
const ACCEPT = '.txt,.md,.markdown,.csv,.json,.yml,.yaml,.rtf,.log,.vtt,.srt,.docx,text/*'
const MAX_CHARS = 200000

export default function CampaignBrief({ onApply, current }) {
  const [text, setText] = useState('')
  const [files, setFiles] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const [answers, setAnswers] = useState({})
  const [open, setOpen] = useState(false)
  const [dropping, setDropping] = useState(false)
  const inputRef = useRef(null)

  function readB64(file) {
    return new Promise((res, rej) => {
      const r = new FileReader()
      r.onerror = () => rej(new Error(`Could not read ${file.name}`))
      r.onload = () => res(String(r.result).split(',')[1] || '')
      r.readAsDataURL(file)
    })
  }

  async function addFiles(list) {
    const next = []
    for (const f of Array.from(list || []).slice(0, 3)) {
      const ok = TEXT_EXT.test(f.name) || BINARY_EXT.test(f.name)
        || f.type.startsWith('text/')
      if (!ok) {
        setError(`${f.name} isn't a transcript or a text spec — paste the relevant `
          + 'part instead.')
        continue
      }
      try {
        if (BINARY_EXT.test(f.name)) {
          next.push({ name: f.name, content_b64: await readB64(f) })
        } else {
          const t = await f.text()
          if (!t.trim()) { setError(`${f.name} has no readable text.`); continue }
          next.push({ name: f.name, text: t.slice(0, MAX_CHARS) })
        }
      } catch (e) { setError(e.message) }
    }
    if (next.length) { setFiles((prev) => [...prev, ...next].slice(0, 3)); setError(null) }
  }

  async function run(withAnswers) {
    setBusy(true); setError(null)
    try {
      const r = await api.campaignBrief({
        text: text.trim(), attachments: files,
        answers: withAnswers ?? answers, current,
      })
      if (r.ok === false) { setError(r.error); setResult(null); return }
      setResult(r)
      // Apply immediately: the whole point is watching the form fill in. Nothing is
      // saved — the user still reviews every field and presses Create.
      if (r.config && Object.keys(r.config).length) onApply(r.config)
    } catch (e) {
      setError(e.status === 501
        ? 'Campaign configuration needs an Anthropic API key on the server.'
        : e.message)
    } finally { setBusy(false) }
  }

  function answer(qid, option) {
    const next = { ...answers, [qid]: option.label }
    setAnswers(next)
    if (option.config && Object.keys(option.config).length) onApply(option.config)
    run(next)
  }

  const canRun = !!text.trim() || files.length > 0

  if (!open) {
    return (
      <div className="brief-teaser">
        <button type="button" className="ghost sm" onClick={() => setOpen(true)}>
          ✦ Describe it instead
        </button>
        <span className="hint">
          Paste the meeting note or drop the spec — the fields below fill themselves in.
        </span>
      </div>
    )
  }

  return (
    <div className={'brief' + (dropping ? ' dropping' : '')}
      onDragOver={(e) => { e.preventDefault(); setDropping(true) }}
      onDragLeave={() => setDropping(false)}
      onDrop={(e) => { e.preventDefault(); setDropping(false); addFiles(e.dataTransfer.files) }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ fontSize: 13, fontWeight: 600 }}>Describe this campaign</div>
        <button type="button" className="ghost sm" onClick={() => setOpen(false)}>
          Fill the form myself
        </button>
      </div>
      <p className="muted" style={{ fontSize: 11.5, margin: '4px 0 8px' }}>
        What did you decide — who are we going after, and what are we saying to them?
        Paste the meeting transcript if that's easier; timecodes and small talk get
        stripped. Everything below gets set from this, and you can still change it.
      </p>

      <ErrorBanner error={error} />

      <textarea rows={4} value={text} placeholder={EXAMPLES[0]}
        onChange={(e) => setText(e.target.value)}
        style={{ width: '100%', resize: 'vertical' }} />

      <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
        <input ref={inputRef} type="file" multiple hidden
          accept={ACCEPT}
          onChange={(e) => { addFiles(e.target.files); e.target.value = '' }} />
        <button type="button" className="ghost sm" onClick={() => inputRef.current?.click()}>
          Attach a transcript or spec
        </button>
        <button type="button" className="primary sm" disabled={busy || !canRun}
          onClick={() => run()}>
          {busy ? <Spinner /> : result ? 'Reconfigure' : 'Configure campaign'}
        </button>
        {files.map((f, i) => (
          <span key={f.name + i} className="badge"
            title={f.text ? `${f.text.length} chars` : 'will be read on the server'}>
            {f.name}
            <button type="button" className="x"
              onClick={() => setFiles(files.filter((_, j) => j !== i))}>×</button>
          </span>
        ))}
      </div>

      {!result && !busy && (
        <div className="brief-examples">
          {EXAMPLES.map((ex) => (
            <button key={ex} type="button" onClick={() => setText(ex)}>{ex}</button>
          ))}
        </div>
      )}

      {result?.summary && (
        <div className="banner info" style={{ marginTop: 12, marginBottom: 0 }}>
          {result.summary}
        </div>
      )}

      {/* The question is the point, not a fallback. Options carry their own field
          overlays, so answering changes the form rather than just the conversation. */}
      {(result?.questions || []).map((q) => (
        <div key={q.id} className="brief-q">
          <div className="brief-q-h">{q.question}</div>
          {q.why && <div className="muted" style={{ fontSize: 11 }}>{q.why}</div>}
          <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
            {q.options.map((o) => (
              <button key={o.label} type="button" disabled={busy}
                className={answers[q.id] === o.label ? 'primary sm' : 'ghost sm'}
                title={o.detail} onClick={() => answer(q.id, o)}>{o.label}</button>
            ))}
          </div>
        </div>
      ))}

      {(result?.notes || []).length > 0 && (
        <ul className="brief-notes">
          {result.notes.map((n, i) => <li key={i}>{n}</li>)}
        </ul>
      )}
      {/* Anything the proposal asked for that the console does not accept. Shown
          rather than swallowed: a filter silently dropped would target the wrong
          accounts, and this screen exists to be inspectable. */}
      {(result?.warnings || []).length > 0 && (
        <ul className="brief-notes warn">
          {result.warnings.map((w, i) => <li key={i}>{w}</li>)}
        </ul>
      )}
    </div>
  )
}
