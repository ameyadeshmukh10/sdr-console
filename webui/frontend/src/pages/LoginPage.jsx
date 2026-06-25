import { useState } from 'react'
import { useAuth } from '../AuthContext.jsx'
import { ErrorBanner } from '../components/ui.jsx'

// EverWorker brand lockup: a mint monogram tile + Gilroy wordmark.
// `light` flips the wordmark to white for use on the dark art panel.
function Wordmark({ light = false }) {
  return (
    <span className={'ev-logo' + (light ? ' ev-logo--light' : '')}>
      <span className="ev-mark" aria-hidden="true">e</span>
      <span className="ev-word">Ever<span className="accent">Worker</span></span>
    </span>
  )
}

// Split-screen login: a clean white form panel on the left, a brand-green
// ocean on the right. The only entry point to the app until the user signs in.
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
    <div className="login">
      {/* Left — wordmark, pitch, and the sign-in form on calm whitespace */}
      <section className="login__form-panel">
        <header className="login__brand">
          <Wordmark />
        </header>

        <div className="login__center">
          <div className="login__intro">
            <h1 className="login__headline">
              Generate more<br /><em>pipeline.</em>
            </h1>
            <p className="login__tagline">
              Sign in to the SDR Console — source, write, and enroll
              value-first outbound on autopilot.
            </p>
          </div>

          <form className="login__form" onSubmit={submit}>
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
            <button className="login__btn" type="submit" disabled={busy || !email || !password}>
              {busy ? 'Signing in…' : 'Log in'}
            </button>
            <p className="login__hint">Trouble signing in? Ping your EverWorker admin.</p>
          </form>
        </div>

        <footer className="login__legal">© EverWorker · SDR Console</footer>
      </section>

      {/* Right — ocean imagery under a brand-green overlay */}
      <section className="login__art" aria-hidden="true">
        <div className="login__art-base" />
        <div className="login__art-photo" />
        <div className="login__art-tint" />
        <div className="login__art-caption">
          <Wordmark light />
          <span className="login__art-by">SDR Console</span>
        </div>
      </section>
    </div>
  )
}
