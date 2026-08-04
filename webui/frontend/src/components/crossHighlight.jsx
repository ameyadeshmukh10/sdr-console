import { useCallback, useEffect, useState } from 'react'

// Cross-widget highlighting for Analytics and Trends.
//
// Both pages stack several widgets that are cuts of the SAME underlying rows: a
// stage bar, a motion tile and an attributed-deal table are three views of one set
// of deals; a campaign appears in the funnel, in the campaign table and as a bar in
// a chart. Answering "where does this one sit in the others?" meant reading across
// them by eye. Picking a value in any widget now marks the matching part of every
// other one and dims the rest.
//
// Two rules this deliberately follows:
//
//   HIGHLIGHT, DON'T FILTER. A row that contributes nothing to the selection is
//   itself a finding; dropping it hides that. Nothing here changes a number — the
//   totals above a table keep describing the whole population, which is what makes
//   the highlighted share readable AS a share.
//
//   ONLY MAKE IT CLICKABLE WHEN IT LINKS SOMEWHERE. A row that highlights only
//   itself teaches people the interaction does nothing, so widgets whose dimension
//   has no counterpart on the page stay static.

// A selection is {dim, value, label}. `dim` names the DIMENSION rather than the
// widget, so any widget able to express that dimension can both set and reflect it
// — that is what makes the link bidirectional without wiring pairs together.
export function useHighlight() {
  const [sel, setSel] = useState(null)

  // Re-picking the same value clears it: the control that turned the highlight on
  // is the one people reach for to turn it off.
  const pick = useCallback((dim, value, label) => {
    setSel((s) => (s && s.dim === dim && s.value === value
      ? null
      : { dim, value, label: label ?? String(value) }))
  }, [])
  const clear = useCallback(() => setSel(null), [])

  useEffect(() => {
    if (!sel) return undefined
    const esc = (e) => { if (e.key === 'Escape') setSel(null) }
    document.addEventListener('keydown', esc)
    return () => document.removeEventListener('keydown', esc)
  }, [sel])

  // '' when nothing is selected, so an un-highlighted page carries no extra
  // classes at all and looks exactly as it did before.
  //
  // `reflects` names the dimension(s) this widget can express. A widget that cannot
  // express the current one stays untouched: picking a campaign must not dim the
  // funnel's stage totals, which are page-level figures with nothing to say about
  // one campaign — dimming them reads as "none of these match", which is false.
  const on = useCallback((isMatch, reflects) => {
    if (!sel) return ''
    if (reflects) {
      const dims = Array.isArray(reflects) ? reflects : [reflects]
      if (!dims.includes(sel.dim)) return ''
    }
    return isMatch ? ' xh-on' : ' xh-off'
  }, [sel])
  const is = useCallback(
    (dim, value) => !!sel && sel.dim === dim && sel.value === value,
    [sel],
  )

  return { sel, pick, clear, on, is }
}

// What is highlighted, what it adds up to, and how to get out.
//
// The summary is the payoff: "Proposal — 3 deals · $132,000" is the answer to the
// question the click was asking, and without it the interaction is only decoration.
export function SelectionBar({ sel, clear, summary, hint }) {
  if (!sel) {
    return hint ? <p className="xh-hint" style={{ margin: '0 0 14px' }}>{hint}</p> : null
  }
  return (
    <div className="selbar" role="status">
      <span className="selbar-k">Highlighting</span>
      <span className="selbar-v">{sel.label}</span>
      {summary && <span className="selbar-sum">· {summary}</span>}
      <span className="grow" />
      <button type="button" onClick={clear}>Clear ✕</button>
    </div>
  )
}

// Row props for a clickable, highlightable table row. Keeps the keyboard path and
// the aria state in one place instead of on every call site.
//
// `reflects` defaults to the dimension the row SETS, which is right for a table
// that only speaks one dimension. Pass it explicitly where a row can be marked by
// more than one — an attributed deal responds to stage, motion and deal alike.
export function rowProps({ on, pick, dim, value, label, isMatch, reflects }) {
  return {
    className: ('xh-pick' + on(isMatch, reflects ?? dim)).trim(),
    onClick: () => pick(dim, value, label),
    onKeyDown: (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(dim, value, label) }
    },
    tabIndex: 0,
    role: 'button',
    'aria-pressed': isMatch,
  }
}
