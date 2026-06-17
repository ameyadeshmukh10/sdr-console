import { NavLink, Route, Routes } from 'react-router-dom'
import UsePage from './pages/UsePage.jsx'
import PipelinePage from './pages/PipelinePage.jsx'
import DiagramPage from './pages/DiagramPage.jsx'
import AnalyticsPage from './pages/AnalyticsPage.jsx'
import TrendsPage from './pages/TrendsPage.jsx'
import OutreachPage from './pages/OutreachPage.jsx'

const NAV = [
  { to: '/', ico: '▶', label: 'Use', end: true },
  { to: '/pipeline', ico: '⟳', label: 'Pipeline' },
  { to: '/diagram', ico: '◉', label: 'Orchestration' },
  { to: '/analytics', ico: '▦', label: 'Analytics' },
  { to: '/trends', ico: '★', label: 'Trends' },
  { to: '/outreach', ico: '✉', label: 'Outreach' },
]

export default function App() {
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          SDR Console
          <small>EverWorker AI SDR</small>
        </div>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end}
            className={({ isActive }) => 'nav-link' + (isActive ? ' active' : '')}>
            <span className="ico">{n.ico}</span>{n.label}
          </NavLink>
        ))}
        <div className="spacer" />
        <div className="foot">Local MVP &middot; read-only<br />+ HubSpot ingest</div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<UsePage />} />
          <Route path="/pipeline" element={<PipelinePage />} />
          <Route path="/diagram" element={<DiagramPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/trends" element={<TrendsPage />} />
          <Route path="/outreach" element={<OutreachPage />} />
        </Routes>
      </main>
    </div>
  )
}
