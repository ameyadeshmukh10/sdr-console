import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api, getDemoProfile, setDemoProfile } from './api.js'

// Global demo mode. One profile is active at a time (null = live data); every API
// request carries it via api.js, so switching re-points the ENTIRE console at that
// synthetic dataset rather than one chart.
//
// Selecting a profile forces a full remount of the routed views (see the `key` on
// the Routes element in App.jsx) — every page loads its data on mount, and without
// that they would keep showing whatever they fetched under the previous mode.
const DemoContext = createContext(null)

export function DemoProvider({ children }) {
  // Read localStorage synchronously so a reload inside a demo doesn't flash live
  // numbers on first paint.
  const [profileId, setProfileId] = useState(() => getDemoProfile())
  const [profiles, setProfiles] = useState([])
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState(null)

  useEffect(() => {
    api.demoProfiles()
      .then((d) => {
        const list = d.profiles || []
        setProfiles(list)
        setLoadError(null)
        // A profile can disappear between sessions (regenerated, renamed, or the
        // volume was reset). Drop back to live rather than 400-ing every request.
        if (profileId && !list.some((p) => p.id === profileId)) {
          setDemoProfile(null)
          setProfileId(null)
        }
      })
      .catch((e) => {
        setProfiles([])
        // Distinguish "none exist" from "the call failed" — the fixes differ
        // (generate one vs. restart a stale backend), so the UI must not show
        // the same dead row for both. A 404 means this backend predates the
        // endpoint entirely.
        setLoadError(e?.status === 404
          ? 'The server does not have /api/demo/profiles — restart it to pick up backend changes.'
          : `Could not load demo profiles (${e?.message || 'unknown error'}).`)
        // Belt and braces for the same failure the server now guards against: if
        // anything about the stored profile makes requests fail, drop to live
        // rather than leaving the console stuck on an unusable selection.
        if (e?.status === 400 && profileId) {
          setDemoProfile(null)
          setProfileId(null)
        }
      })
      .finally(() => setLoaded(true))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const select = useCallback((id) => {
    setDemoProfile(id)
    setProfileId(id || null)
  }, [])

  const active = profileId ? profiles.find((p) => p.id === profileId) || { id: profileId } : null

  return (
    <DemoContext.Provider value={{ profileId, profiles, active, select, loaded, loadError }}>
      {children}
    </DemoContext.Provider>
  )
}

export function useDemo() {
  return useContext(DemoContext) || { profileId: null, profiles: [], active: null, select: () => {}, loaded: false, loadError: null }
}

// Does the active profile claim to cover this area? Views use it to tell
// "this profile has no data for me" apart from "there is genuinely no data".
export function useCovers(area) {
  const { active } = useDemo()
  if (!active) return true
  return (active.covers || []).includes(area)
}
