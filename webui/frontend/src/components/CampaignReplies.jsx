import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import ContactLink from './ContactLink.jsx'

// What came back, from this campaign's contacts.
//
// Matched on EMAIL: a reply arrives from a mailbox and carries no campaign id, so
// the member's address is the only join available. It is exact — an address either
// belongs to a member of this campaign or it does not.
//
// Deliberately read-only. Working a reply (drafting, approving, sending) stays on
// the Replies view, which is built for it and is where the guardrails live; this
// answers "did this campaign land?" and then hands over.
const INTENT_TONE = {
  interested: 'status-enrolled', meeting: 'status-enrolled',
  not_interested: 'status-failed', unsubscribe: 'status-failed',
  referral: 'status-skipped', ooo: 'status-pending',
}

export default function CampaignReplies({ campaignId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.campaignReplies(campaignId)
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
  }, [campaignId])

  if (error) return <ErrorBanner error={error} />
  if (!data) return <Spinner label="Loading replies…" />

  const rows = data.replies || []

  return (
    <div>
      <div className="card-h" style={{ marginBottom: 10 }}>
        <div>
          <p className="card-note" style={{ marginTop: 0 }}>
            Replies from people in this campaign, newest first. Drafting and sending
            happen on the Replies view.
          </p>
        </div>
        <div className="card-meta"><b>{num(rows.length)}</b> replies</div>
      </div>

      {rows.length === 0 ? (
        <div className="empty">
          No replies from this campaign's contacts yet.
          {data.members_with_email
            ? ` Watching ${num(data.members_with_email)} addresses.`
            : ' None of these members has an email address on file.'}
        </div>
      ) : (
        <>
          <div className="toolbar">
            <div className="grow" />
            <Link className="ghost sm btn-link" to={`/replies?campaign=${campaignId}`}>
              Work these replies →
            </Link>
          </div>
          <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
            <table className="dense" style={{ width: '100%', minWidth: 820 }}>
              <thead><tr>
                <th style={{ width: '16%' }}>From</th>
                <th style={{ width: '14%' }}>Company</th>
                <th style={{ width: '9%' }}>Channel</th>
                <th style={{ width: '11%' }}>Intent</th>
                <th style={{ width: '10%' }}>When</th>
                <th style={{ width: '30%' }}>What they said</th>
                <th style={{ width: '10%' }}>Draft</th>
              </tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.reply_id}>
                    <td><ContactLink contact={{ contact_id: r.contact_id,
                      first_name: (r.from_name || '').split(' ')[0],
                      last_name: (r.from_name || '').split(' ').slice(1).join(' ') }} /></td>
                    <td className="muted">{r.company || '—'}</td>
                    <td><span className="badge">{r.channel}</span></td>
                    <td>{r.intent
                      ? <span className={`badge ${INTENT_TONE[r.intent] || ''}`}>
                        {r.intent.replace(/_/g, ' ')}</span>
                      : <span className="muted">—</span>}</td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {(r.date_received || '').slice(0, 10) || '—'}
                    </td>
                    <td className="muted">
                      <Link to={`/replies?reply=${encodeURIComponent(r.reply_id)}`}
                        className="reply-open" title="Open this reply on the Replies view">
                        <span className="clamp2">{r.snippet || 'open reply'}</span>
                      </Link>
                    </td>
                    <td>{r.draft_status
                      ? <span className={'badge ' + (r.draft_status === 'sent'
                        ? 'status-enrolled' : 'status-pending')}>{r.draft_status}</span>
                      : <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
