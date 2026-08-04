import { useEffect, useState } from 'react'
import { NavLink, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api.js'
import { useAuth } from './AuthContext.jsx'
import { DemoProvider, useDemo } from './DemoContext.jsx'
import { AddonProvider } from './components/Addon.jsx'
import { BrandLogo } from './components/BrandLogo.jsx'
import DemoSwitcher from './components/DemoSwitcher.jsx'
import LoginPage from './pages/LoginPage.jsx'
import HomePage from './pages/HomePage.jsx'
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
  '/': <svg {...ICO}><path d="M3 10.5 12 3l9 7.5" /><path d="M5 9.5V21h14V9.5" /></svg>,
  '/use': <svg {...ICO}><polygon points="6 4 20 12 6 20 6 4" /></svg>,
  '/pipeline': <svg {...ICO}><path d="M21 12a9 9 0 1 1-3-6.7" /><polyline points="21 4 21 9 16 9" /></svg>,
  '/diagram': <svg {...ICO}><circle cx="12" cy="12" r="3.2" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09A1.7 1.7 0 0 0 8.9 19.3a1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.7 8.9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34H9.1A1.7 1.7 0 0 0 10.1 3.1V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.08a1.7 1.7 0 0 0 1.56 1.03H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.56 1.03z" /></svg>,
  '/analytics': <svg {...ICO}><line x1="6" y1="20" x2="6" y2="11" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="18" y1="20" x2="18" y2="14" /></svg>,
  '/trends': <svg {...ICO}><polyline points="3 16 9 10 13 14 21 6" /><polyline points="15 6 21 6 21 12" /></svg>,
  '/replies': <svg {...ICO}><path d="M21 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2z" /></svg>,
  '/signals': <svg {...ICO}><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z" /></svg>,
  '/outreach': <svg {...ICO}><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></svg>,
}

// Sidebar structure: standalone links plus collapsible groups. `items` marks a
// group; everything else renders as a plain top-level link.
const NAV = [
  { to: '/', label: 'Home', end: true },
  {
    group: 'action-center',
    label: 'Action Center',
    items: [
      { to: '/use', label: 'Use' },
      { to: '/pipeline', label: 'Pipeline' },
      { to: '/signals', label: 'Signals' },
      { to: '/outreach', label: 'Outreach' },
      { to: '/replies', label: 'Replies' },
    ],
  },
  {
    group: 'reporting',
    label: 'Reporting',
    items: [
      { to: '/analytics', label: 'Analytics' },
      { to: '/trends', label: 'Trends' },
    ],
  },
]

// Pinned to the bottom of the rail, below the spacer.
const FOOT_NAV = [
  { to: '/diagram', label: 'Setup' },
]

const Chevron = () => (
  <svg className="chev" width="14" height="14" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="9 6 15 12 9 18" />
  </svg>
)

function NavItem({ to, label, end }) {
  return (
    <NavLink to={to} end={end}
      className={({ isActive }) => 'nav-link' + (isActive ? ' active' : '')}>
      <span className="ico">{ICONS[to]}</span>{label}
    </NavLink>
  )
}

// Collapsed by default; opens on click and auto-opens whenever the current
// route lives inside the group (so a deep link never lands on a hidden item).
function NavGroup({ label, items }) {
  const { pathname } = useLocation()
  const holdsActive = items.some((i) => pathname === i.to || pathname.startsWith(i.to + '/'))
  const [open, setOpen] = useState(holdsActive)
  useEffect(() => { if (holdsActive) setOpen(true) }, [holdsActive])
  return (
    <div className={'nav-group' + (open ? ' open' : '')}>
      <button type="button" className="nav-group-toggle" aria-expanded={open}
        onClick={() => setOpen((v) => !v)}>
        <Chevron />
        <span className="nav-group-label">{label}</span>
      </button>
      {open && (
        <div className="nav-group-items">
          {items.map((i) => <NavItem key={i.to} {...i} />)}
        </div>
      )}
    </div>
  )
}

// One-time durability check: if the server says the data dir looks non-durable
// (no Railway Volume at /app/data), warn on every page — everything the console
// records (dedup ledgers, queues, dismissals) is lost on redeploy until fixed.
function VolumeBanner() {
  const [sys, setSys] = useState(null)
  useEffect(() => { api.systemStatus().then(setSys).catch(() => {}) }, [])
  if (!sys?.volume_suspect) return null
  return (
    <div className="banner warn" style={{ marginBottom: 20 }}>
      <b>Data volume looks non-persistent.</b> Attach a Railway Volume mounted at{' '}
      <code>/app/data</code> (Railway dashboard → this service → Settings → Volumes → Attach),
      or everything recorded here — HubSpot dedup ledger, reply queues, dismissals — resets on
      every redeploy and activity re-logs to HubSpot as duplicates.
    </div>
  )
}

export default function App() {
  const { token, email, logout } = useAuth()
  if (!token) return <LoginPage />
  return (
    <DemoProvider>
      <AddonProvider>
        <AppShell email={email} logout={logout} />
      </AddonProvider>
    </DemoProvider>
  )
}

function AppShell({ email, logout }) {
  const { profileId } = useDemo()
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <BrandLogo variant="white" />
          <small>SDR Console</small>
        </div>
        {NAV.map((n) => (
          n.items
            ? <NavGroup key={n.group} label={n.label} items={n.items} />
            : <NavItem key={n.to} {...n} />
        ))}
        <div className="spacer" />
        {FOOT_NAV.map((n) => <NavItem key={n.to} {...n} />)}
        <DemoSwitcher />
        <div className="signed-in">
          <span className="who" title={email}>{email}</span>
          <button className="signout" onClick={logout}>Sign out</button>
        </div>
      </aside>
      <main className="main">
        <VolumeBanner />
        {/* Remount every view when the data source changes — pages fetch on mount,
            so without this they would keep rendering the previous mode's data. */}
        <Routes key={profileId || 'live'}>
          <Route path="/" element={<HomePage />} />
          <Route path="/use" element={<UsePage />} />
          <Route path="/pipeline" element={<PipelinePage />} />
          <Route path="/diagram" element={<DiagramPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/trends" element={<TrendsPage />} />
          <Route path="/replies" element={<RepliesPage />} />
          <Route path="/signals" element={<SignalsPage />} />
          <Route path="/outreach" element={<OutreachPage />} />
          {/* Campaigns live inside Use rather than on their own page — starting one
              IS how you put the worker to work. These keep older links alive and
              give Home's widget somewhere specific to point. */}
          <Route path="/campaigns" element={<UsePage initialTab="campaigns" />} />
          <Route path="/calllist" element={<UsePage initialTab="calllist" />} />
        </Routes>
      </main>
    </div>
  )
}
