// Left pane of the Replies inbox: sectioned message list (Interested / Possible /
// Follow up / Other / Dismissed), one row per reply, email-client style. Section
// headers toggle collapse; the count stays visible while collapsed.

export const INTENT_COLOR = {
  meeting_request: 'var(--green)', info_request: 'var(--accent)', pricing: 'var(--purple)',
  positive_later: 'var(--green)', positive_other: 'var(--green)', referral: 'var(--amber)',
  not_interested: 'var(--muted)', auto_reply: 'var(--muted)', unsubscribe: 'var(--red)', error: 'var(--red)',
}

function initials(it) {
  const name = it.from_name || `${it.first_name || ''} ${it.last_name || ''}`.trim() || it.from_email || '?'
  const parts = name.replace(/@.*/, '').split(/[\s._-]+/).filter(Boolean)
  return ((parts[0]?.[0] || '?') + (parts[1]?.[0] || '')).toUpperCase()
}

function when(d) {
  if (!d) return ''
  const t = new Date(d)
  if (Number.isNaN(t.getTime())) return ''
  const days = (Date.now() - t.getTime()) / 86400000
  return days < 1 ? t.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : t.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function Row({ it, active, dim, onSelect }) {
  const cls = it.classifier || {}
  const li = it.channel === 'linkedin'
  return (
    <div className={`inbox-row${active ? ' active' : ''}${dim ? ' dim' : ''}`}
      onClick={() => onSelect(it)}>
      <div className="avatar">{initials(it)}</div>
      <div className="who">
        {it.from_name || `${it.first_name || ''} ${it.last_name || ''}`.trim() || it.from_email || 'LinkedIn lead'}
      </div>
      <div className="when">{when(it.date_received)}</div>
      <div className="snippet">
        {(it.title || it.company) && <b style={{ color: 'var(--faint)' }}>{[it.title, it.company].filter(Boolean).join(' · ')} — </b>}
        {(it.text_body || '').slice(0, 160)}
      </div>
      <div className="meta">
        <span className="intent-dot" style={{ background: INTENT_COLOR[cls.intent] || 'var(--border-strong)' }} />
        <span>{cls.intent || '—'}</span>
        {cls.confidence != null && <span>{Math.round(cls.confidence * 100)}%</span>}
        <span>{li ? 'in LinkedIn' : '✉ email'}</span>
        {it.handled && <span style={{ color: 'var(--green)' }}>{it.parked ? 'parked' : 'replied ✓'}</span>}
        {it.dismissed && <span>dismissed</span>}
        {it.reclassified && <span style={{ color: 'var(--amber)' }}>reclassified</span>}
      </div>
    </div>
  )
}

export default function InboxList({ sections, selectedId, onSelect, collapsed = {}, onToggle }) {
  return (
    <div className="inbox-list">
      {sections.map((s) => (s.items.length === 0 && s.hideWhenEmpty ? null : (
        <div key={s.key}>
          <div className="inbox-section" style={{ cursor: 'pointer', userSelect: 'none' }}
            onClick={() => onToggle && onToggle(s.key)}>
            <span>
              <span style={{ display: 'inline-block', width: 13 }}>{collapsed[s.key] ? '▸' : '▾'}</span>
              {s.label} ({s.items.length})
            </span>
            {s.action && <span onClick={(e) => e.stopPropagation()}>{s.action}</span>}
          </div>
          {collapsed[s.key] ? null : s.items.length === 0
            ? <div className="muted" style={{ padding: '10px 16px', fontSize: 12 }}>{s.empty || 'Nothing here.'}</div>
            : s.items.map((it) => (
              <Row key={`${it.channel}-${it.reply_id}`} it={it} dim={s.dim}
                active={String(it.reply_id) === String(selectedId)} onSelect={onSelect} />
            ))}
        </div>
      )))}
    </div>
  )
}
