import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { Badge, Spinner, ErrorBanner } from './ui.jsx'
import { Link } from 'react-router-dom'

// Slide-over drawer showing one lead's full 4-touch email sequence + LinkedIn copy.
export default function OutreachDetail({ id, onClose }) {
  const [d, setD] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    setD(null); setError(null)
    api.outreachDetail(id).then(setD).catch((e) => setError(e.message))
  }, [id])

  const seq = d?.sequence
  const stateOf = (list, n) => list?.find((x) => x.step === n)?.state || 'draft'
  const emailSteps = d ? [1, 2, 3, 4].map((i) => ({
    n: i, subject: d.email[`subject${i}`], body: d.email[`body${i}`],
    state: stateOf(seq?.email, i),
  })) : []
  const liSteps = d ? [
    ['Connection request', d.linkedin.li_connect, stateOf(seq?.linkedin, 1)],
    ['Message 1', d.linkedin.li_msg1, stateOf(seq?.linkedin, 2)],
    ['Message 2', d.linkedin.li_msg2, stateOf(seq?.linkedin, 3)],
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

            {/* The person, not the campaign you arrived from. Membership lives on
                the contact: someone worked by three campaigns looks unrelated on
                each screen unless all three travel with them — which is also how a
                prospect quietly gets triple-touched. */}
            {(d.campaigns || []).length > 0 && (
              <div className="contact-campaigns">
                <span className="k">In {d.campaigns.length === 1 ? 'campaign' : 'campaigns'}</span>
                <span className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                  {d.campaigns.map((c) => (
                    <Link key={c.campaign_id} className="badge camp-tag"
                      to={`/campaigns?open=${c.campaign_id}`}
                      title={`${c.name} — ${c.state}${c.score != null ? ` · score ${Math.round(c.score)}` : ''}`}>
                      {c.name}
                      <span className="muted"> · {c.state}</span>
                    </Link>
                  ))}
                </span>
                {d.campaigns.length > 1 && (
                  <span className="hint" style={{ flexBasis: '100%', marginTop: 4 }}>
                    Their touches across these are interleaved on one schedule, so the
                    copy reads as one conversation.
                  </span>
                )}
              </div>
            )}
            {d.engagement && (
              <div className={'banner ' + (d.engagement.engagement_state === 'suppressed'
                ? 'warn' : 'info')} style={{ marginTop: 10, marginBottom: 0 }}>
                {d.engagement.engagement_state === 'suppressed'
                  ? 'Do not contact — this person is suppressed everywhere.'
                  : `All outreach paused${d.engagement.paused_until
                    ? ` until ${d.engagement.paused_until}` : ''}.`}
                {d.engagement.engagement_note ? ` ${d.engagement.engagement_note}` : ''}
              </div>
            )}

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

            <div className="row between" style={{ alignItems: 'baseline' }}>
              <div className="section-h">Email sequence</div>
              {seq && (
                <span className="muted" style={{ fontSize: 11.5 }}>
                  {seq.email_sent} of {seq.email_total} sent
                </span>
              )}
            </div>
            {emailSteps.map((s) => (
              <div className="touch" key={s.n}>
                <div className="row between" style={{ alignItems: 'center' }}>
                  <div className="step">Touch {s.n}</div>
                  <span className={'msg-state ' + s.state}>{s.state}</span>
                </div>
                <div className="subj">{s.subject}</div>
                <div className="body">{s.body}</div>
              </div>
            ))}

            <div className="row between" style={{ alignItems: 'baseline' }}>
              <div className="section-h">LinkedIn</div>
              {seq && (
                <span className="muted" style={{ fontSize: 11.5 }}>
                  {seq.li_sent} of {seq.li_total} sent
                </span>
              )}
            </div>
            {liSteps.map(([label, text, state]) => (
              <div className="touch" key={label}>
                <div className="row between" style={{ alignItems: 'center' }}>
                  <div className="step">{label}</div>
                  <span className={'msg-state ' + state}>{state}</span>
                </div>
                <div className="body">{text || <span className="muted">—</span>}</div>
              </div>
            ))}

            {seq && (
              <p className="muted" style={{ fontSize: 11.5, marginTop: 12, lineHeight: 1.5 }}>
                Send state comes from the activity log. Steps run in order, so the
                count of logged touches determines which are marked sent — it is not a
                per-message delivery receipt.
                {seq.last_sent_at && <> Last send {new Date(seq.last_sent_at).toLocaleString()}.</>}
                {seq.replied && <> This contact has replied.</>}
              </p>
            )}
          </>
        )}
      </div>
    </>
  )
}
