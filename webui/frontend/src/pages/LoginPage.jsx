import { useState } from 'react'
import { useAuth } from '../AuthContext.jsx'
import { ErrorBanner } from '../components/ui.jsx'

// Deck slide-1 aesthetic: dark emerald field, EverWorker wordmark, a clean
// white card. The only entry point to the app until the user signs in.
export default function LoginPage() {
  const { login } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    if (busy) return
    setError(null)
    setBusy(true)
    try {
      await login(email.trim(), password)
      // success unmounts this page (App re-renders the authed layout)
    } catch (err) {
      setError('That email and password didn’t match. Please try again.')
      setBusy(false)
    }
  }

  return (
    <div className="login-screen">
      <div className="login-glow" />
      <div className="login-content">
        <div className="login-logo">Ever<span className="mark">Worker</span></div>
        <form className="login-card" onSubmit={submit}>
          <h1 className="login-title">Let’s get you more pipeline.</h1>
          <p className="login-sub">Sign in to the SDR Console.</p>
          <ErrorBanner error={error} />
          <label className="field">
            Email
            <input
              type="email" autoFocus autoComplete="username"
              placeholder="you@everworker.ai" required
              value={email} onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label className="field">
            Password
            <input
              type="password" autoComplete="current-password"
              placeholder="••••••••" required
              value={password} onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <button className="login-btn" type="submit" disabled={busy || !email || !password}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        <p className="login-foot">EverWorker · SDR Console</p>
      </div>
    </div>
  )
}
