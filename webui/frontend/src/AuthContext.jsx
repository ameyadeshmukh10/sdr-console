import { createContext, useContext, useEffect, useState } from 'react'
import { api, getToken, setToken, setOnUnauthorized } from './api'

// Minimal auth state for the SDR Console login gate. The token lives in
// localStorage (owned by api.js); the email is kept alongside it for display.
const EMAIL_KEY = 'sdr_auth_email'
const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  // Initialize synchronously from localStorage so an already-signed-in user
  // never sees a one-frame flash of the login screen on first paint.
  const [auth, setAuth] = useState(() => ({
    token: getToken(),
    email: localStorage.getItem(EMAIL_KEY) || '',
  }))

  function logout() {
    setToken(null)
    localStorage.removeItem(EMAIL_KEY)
    setAuth({ token: null, email: '' })
  }

  async function login(email, password) {
    const res = await api.login(email, password)
    setToken(res.token)
    localStorage.setItem(EMAIL_KEY, res.email)
    setAuth({ token: res.token, email: res.email })
    return res
  }

  // When the server rejects our token mid-session, fall back to the login screen.
  useEffect(() => {
    setOnUnauthorized(logout)
    return () => setOnUnauthorized(null)
  }, [])

  return (
    <AuthContext.Provider value={{ ...auth, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
