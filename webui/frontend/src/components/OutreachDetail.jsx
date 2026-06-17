import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Badge, Spinner, ErrorBanner } from './ui.jsx'

// Slide-over drawer showing one lead's full 4-touch email sequence + LinkedIn copy.
export default function OutreachDetail({ id, onClose }) {
  const [d, setD] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    setD(null); setError(null)
    api.outreachDetail(id).then(setD).catch((e) => setError(e.message))
  }, [id])

  const emailSteps = d ? [1, 2, 3, 4].map((i) => ({
    n: i, subject: d.email[`subject${i}`], body: d.email[`body${i}`],
  })) : []
  const liSteps = d ? [
    ['Connection request', d.linkedin.li_connect],
    ['Message 1', d.linkedin.li_msg1],
    ['Message 2', d.linkedin.li_msg2],
  ] : []

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <div className="drawer">
        <button className="close-x" onClick={onClose}>Close ✕</button>
        <ErrorBanner error={error} />
        {!d && !error && <Spinner label="Loading copy…" />}
        {d && (
          <>
            <h2 className="page-title" style={{ marginBottom: 2 }}>
              {d.contact.first_name} {d.contact.last_name}
            </h2>
            <p className="page-sub" style={{ marginBottom: 14 }}>
              {d.contact.title}{d.contact.company ? ` · ${d.contact.company}` : ''}
            </p>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              <Badge kind="persona" value={d.contact.persona} />
              <span className="badge cta">{d.cta_type}</span>
              <Badge kind="status" value={d.contact.status} />
              {d.contact.batch_id != null && <span className="badge">batch #{d.contact.batch_id}</span>}
            </div>

            <div className="kv">
              <span className="k">Email</span><span className="mono">{d.contact.email}</span>
              {d.contact.linkedin_url && (<>
                <span className="k">LinkedIn</span>
                <a href={d.contact.linkedin_url} target="_blank" rel="noreferrer">{d.contact.linkedin_url}</a>
              </>)}
              {d.contact.error && (<><span className="k">Error</span><span style={{ color: 'var(--amber)' }}>{d.contact.error}</span></>)}
            </div>

            <div className="section-h">Signal</div>
            <div className="touch"><div className="body">{d.signal}</div></div>

            <div className="section-h">Email sequence</div>
            {emailSteps.map((s) => (
              <div className="touch" key={s.n}>
                <div className="step">Touch {s.n}</div>
                <div className="subj">{s.subject}</div>
                <div className="body">{s.body}</div>
              </div>
            ))}

            <div className="section-h">LinkedIn</div>
            {liSteps.map(([label, text]) => (
              <div className="touch" key={label}>
                <div className="step">{label}</div>
                <div className="body">{text || <span className="muted">—</span>}</div>
              </div>
            ))}
          </>
        )}
      </div>
    </>
  )
}
