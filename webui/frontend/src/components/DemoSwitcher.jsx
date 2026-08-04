import { useEffect, useRef, useState } from 'react'
import { useDemo } from '../DemoContext.jsx'

// Sidebar control, pinned under Setup. Collapsed it is a single row showing which
// dataset the console is pointed at; open it lists the profiles on disk.
export default function DemoSwitcher() {
  const { profileId, profiles, active, select, loaded, loadError } = useDemo()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  // Click-away and Escape, so the popover never traps the user.
  useEffect(() => {
    if (!open) return undefined
    const onDown = (e) => { if (!ref.current?.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // Two different dead ends, deliberately worded differently: the list came back
  // empty (generate a profile) vs. the call failed (usually a stale backend that
  // predates the endpoint — restart it). Showing one row for both sends people
  // looking in the wrong place.
  if (loaded && !profiles.length && !profileId) {
    return (
      <div className="demo-switcher">
        <div className={'demo-row muted-row' + (loadError ? ' err' : '')}
          title={loadError || 'Run make_demo_profile.py to create one'}>
          <span className={'demo-dot' + (loadError ? ' err' : ' off')} />
          <span className="demo-label">
            {loadError ? 'Demo unavailable' : 'No demo profiles'}
          </span>
        </div>
      </div>
    )
  }

  function choose(id) {
    select(id)
    setOpen(false)
  }

  return (
    <div className={'demo-switcher' + (profileId ? ' on' : '')} ref={ref}>
      {open && (
        <div className="demo-pop" role="listbox" aria-label="Data source">
          <div className="demo-pop-h">Data source</div>
          <button className={'demo-opt' + (!profileId ? ' sel' : '')}
            role="option" aria-selected={!profileId} onClick={() => choose(null)}>
            <span className="demo-opt-label">Live data</span>
            <span className="demo-opt-sub">Production data</span>
          </button>
          {profiles.map((p) => (
            <button key={p.id} className={'demo-opt' + (profileId === p.id ? ' sel' : '')}
              role="option" aria-selected={profileId === p.id} onClick={() => choose(p.id)}>
              <span className="demo-opt-label">{p.label}</span>
              {p.description && <span className="demo-opt-sub">{p.description}</span>}
              {p.covers?.length > 0 && (
                <span className="demo-opt-covers">{p.covers.join(' · ')}</span>
              )}
            </button>
          ))}
        </div>
      )}
      <button className="demo-row" onClick={() => setOpen((v) => !v)}
        aria-expanded={open} aria-haspopup="listbox">
        <span className={'demo-dot' + (profileId ? '' : ' off')} />
        <span className="demo-label">
          {profileId ? (active?.label || profileId) : 'Live data'}
        </span>
        <span className="demo-caret">▾</span>
      </button>
    </div>
  )
}
