import { useState } from 'react'

// One message = one chat-style bubble: inbound (prospect) left, ours right.
// Long bodies clamp with an expand-in-place toggle — nothing is truncated away.
const CLAMP = 1200

function fmtDate(d) {
  if (!d) return null
  const t = new Date(d)
  return Number.isNaN(t.getTime()) ? d : t.toLocaleString()
}

function Msg({ m, channel }) {
  const [open, setOpen] = useState(false)
  const text = m.text || ''
  const long = text.length > CLAMP
  const shown = open || !long ? text : text.slice(0, CLAMP)
  const kindLabel = m.dir === 'in'
    ? 'their reply'
    : m.kind === 'followup' ? 'our follow-up'
      : channel === 'linkedin' ? 'we sent (DM)' : 'we sent (sequence)'
  return (
    <div className={`thread-msg ${m.dir}${m.kind === 'followup' ? ' followup' : ''}`}>
      <div className="msg-meta">
        <b>{m.from || (m.dir === 'in' ? 'prospect' : 'us')}</b>
        <span>{kindLabel}</span>
        {m.agent && <span>· {m.agent} agent</span>}
        {m.date && <span>{fmtDate(m.date)}</span>}
      </div>
      {m.subject && <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>{m.subject}</div>}
      <div className="msg-body">{shown}{long && !open ? '…' : ''}</div>
      {long && (
        <button className="linklike" style={{ fontSize: 11, marginTop: 6 }} onClick={() => setOpen((v) => !v)}>
          {open ? '▴ Show less' : `▾ Show full message (${text.length.toLocaleString()} chars)`}
        </button>
      )}
    </div>
  )
}

// The merged conversation the backend attaches as item.thread (outbound sequence +
// inbound reply + console follow-ups, chronological). Falls back to composing from
// the raw fields if a stale queue predates the thread merge.
export default function ThreadView({ item }) {
  let thread = item.thread
  if (!thread?.length) {
    thread = [
      ...[...(item.sent_emails || [])].reverse().map((m) => ({
        dir: 'out', kind: 'sequence', subject: m.subject, date: m.date,
        from: m.from_email || item.sending_email, text: m.text || '',
      })),
      { dir: 'in', kind: 'reply', subject: item.subject, date: item.date_received,
        from: item.from_email || item.from_name, text: item.text_body || '' },
    ]
  }
  return (
    <div className="thread">
      {thread.map((m, i) => <Msg key={i} m={m} channel={item.channel} />)}
    </div>
  )
}
