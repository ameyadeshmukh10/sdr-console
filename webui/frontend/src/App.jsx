import { NavLink, Route, Routes } from 'react-router-dom'
import { useAuth } from './AuthContext.jsx'
import { BrandLogo } from './components/BrandLogo.jsx'
import LoginPage from './pages/LoginPage.jsx'
import UsePage from './pages/UsePage.jsx'
import PipelinePage from './pages/PipelinePage.jsx'
import DiagramPage from './pages/DiagramPage.jsx'
import AnalyticsPage from './pages/AnalyticsPage.jsx'
import TrendsPage from './pages/TrendsPage.jsx'
import OutreachPage from './pages/OutreachPage.jsx'
import RepliesPage from './pages/RepliesPage.jsx'
import SignalsPage from './pages/SignalsPage.jsx'

// Inline stroke icons for the nav, keyed by route. `currentColor` lets them
// inherit the existing nav-link color and hover/active states for free.
const ICO = {
  width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 1.6, strokeLinecap: 'round', strokeLinejoin: 'round',
}
const ICONS = {
  '/': <svg {...ICO}><polygon points="6 4 20 12 6 20 6 4" /></svg>,
  '/pipeline': <svg {...ICO}><path d="M21 12a9 9 0 1 1-3-6.7" /><polyline points="21 4 21 9 16 9" /></svg>,
  '/diagram': <svg {...ICO}><circle cx="6" cy="12" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="18" cy="18" r="2.5" /><path d="M8.2 11 15.8 7M8.2 13l7.6 4" /></svg>,
  '/analytics': <svg {...ICO}><line x1="6" y1="20" x2="6" y2="11" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="18" y1="20" x2="18" y2="14" /></svg>,
  '/trends': <svg {...ICO}><polyline points="3 16 9 10 13 14 21 6" /><polyline points="15 6 21 6 21 12" /></svg>,
  '/replies': <svg {...ICO}><path d="M21 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2z" /></svg>,
  '/signals': <svg {...ICO}><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" /></svg>,
  '/outreach': <svg {...ICO}><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></svg>,
}

const NAV = [
  { to: '/', ico: '▶', label: 'Use', end: true },
  { to: '/pipeline', ico: '⟳', label: 'Pipeline' },
  { to: '/diagram', ico: '◉', label: 'Orchestration' },
  { to: '/analytics', ico: '▦', label: 'Analytics' },
  { to: '/trends', ico: '★', label: 'Trends' },
  { to: '/replies', ico: '✦', label: 'Replies' },
  { to: '/signals', ico: '⚡', label: 'Signals' },
  { to: '/outreach', ico: '✉', label: 'Outreach' },
]

export default function App() {
  const { token, email, logout } = useAuth()
  if (!token) return <LoginPage />
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <BrandLogo variant="white" />
          <small>SDR Console</small>
        </div>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}
            className={({ isActive }) => 'nav-link' + (isActive ? ' active' : '')}>
            <span className="ico">{ICONS[n.to]}</span>{n.label}
          </NavLink>
        ))}
        <div className="spacer" />
        <div className="signed-in">
          <span className="who" title={email}>{email}</span>
          <button className="signout" onClick={logout}>Sign out</button>
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<UsePage />} />
          <Route path="/pipeline" element={<PipelinePage />} />
          <Route path="/diagram" element={<DiagramPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/trends" element={<TrendsPage />} />
          <Route path="/replies" element={<RepliesPage />} />
          <Route path="/signals" element={<SignalsPage />} />
          <Route path="/outreach" element={<OutreachPage />} />
        </Routes>
      </main>
    </div>
  )
}
