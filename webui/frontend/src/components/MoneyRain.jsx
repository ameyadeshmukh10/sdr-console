import { useEffect, useState } from 'react'

// The campaign-created celebration.
//
// Same joke as the `$` scale on the call list, cashed in at the moment the money
// actually starts: you finished building a campaign. Dollar signs rain, the line
// lands, it clears itself after a few seconds and never blocks anything — the
// campaign is already open behind it.
//
// Drawn rather than embedded: a remote GIF is blocked by the CSP, and shipping a
// copy of a copyrighted clip inside the bundle is not something to do to a product
// that gets demoed to customers. The line does the work.
//
// Respects prefers-reduced-motion by skipping the animation entirely — a screenful
// of falling glyphs is exactly the thing that setting exists for.

const BILLS = 26
const LIFETIME = 4200

export default function MoneyRain({ onDone }) {
  const [gone, setGone] = useState(false)

  useEffect(() => {
    const t = setTimeout(() => { setGone(true); onDone?.() }, LIFETIME)
    return () => clearTimeout(t)
  }, [onDone])

  if (gone) return null

  const reduced = typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  return (
    <div className="money-rain" aria-hidden="true" onClick={() => setGone(true)}>
      {!reduced && Array.from({ length: BILLS }, (_, i) => (
        <span key={i} className="bill" style={{
          // Deterministic-ish spread rather than a uniform grid: evenly spaced
          // glyphs read as a loading bar, not as money falling.
          left: `${(i * 37 + (i % 5) * 11) % 96}%`,
          animationDelay: `${(i % 9) * 0.13}s`,
          animationDuration: `${2.1 + (i % 6) * 0.28}s`,
          fontSize: `${16 + (i % 4) * 7}px`,
          opacity: 0.5 + (i % 4) * 0.15,
        }}>$</span>
      ))}
      <div className="money-rain-card">
        <div className="money-rain-glyphs">
          {[0, 1, 2, 3, 4].map((i) => (
            <span key={i} style={{ '--i': i }}>$</span>
          ))}
        </div>
        <div className="money-rain-line">Dollar, dollar, bills y’all</div>
        <div className="money-rain-sub">Campaign’s up. Go get paid.</div>
      </div>
    </div>
  )
}
