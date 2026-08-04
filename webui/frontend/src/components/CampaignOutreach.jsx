import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { Spinner, ErrorBanner, num } from './ui.jsx'
import OutreachDetail from './OutreachDetail.jsx'
import ContactLink from './ContactLink.jsx'
import { ScoreBadge, CampaignTags } from './campaignShared.jsx'

// What the agent actually wrote, for this campaign's contacts.
//
// The copy has always existed under /outreach, but only as one flat app-wide list —
// so the obvious question standing on a campaign ("what did we say to these
// people?") meant leaving the campaign, and then filtering by hand. This is the
// same data and the same drawer, scoped to the members.
//
// Contacts with NO copy yet are listed too. "Which of these has the agent written
// to?" is the question, and a list that quietly dropped the un-written ones would
// answer it wrongly by omission.
export default function CampaignOutreach({ campaignId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [openId, setOpenId] = useState(null)
  const [onlyWritten, setOnlyWritten] = useState(false)

  useEffect(() => {
    api.campaignOutreach(campaignId)
      .then((d) => { setData(d); setError(null) })
      .catch((e) => setError(e.message))
  }, [campaignId])

  if (error) return <ErrorBanner error={error} />
  if (!data) return <Spinner label="Loading copy…" />

  const all = data.contacts || []
  const rows = onlyWritten ? all.filter((r) => r.has_copy) : all

  return (
    <div>
      <div className="card-h" style={{ marginBottom: 10 }}>
        <div>
          <p className="card-note" style={{ marginTop: 0 }}>
            The sequence the persona agent wrote for each contact. Click a row for the
            full 4-touch email copy and the LinkedIn track.
          </p>
        </div>
        <div className="card-meta">
          <div><b>{num(data.written)}</b> of {num(data.total)} written</div>
          {data.merged > 0 && (
            <div title={'These contacts are in more than one campaign, so their touches '
              + 'are merged into one de-conflicted schedule rather than sent twice.'}>
              {num(data.merged)} on a merged sequence
            </div>
          )}
        </div>
      </div>

      <div className="toolbar">
        <label className="f-check">
          <input type="checkbox" checked={onlyWritten}
            onChange={(e) => setOnlyWritten(e.target.checked)} />
          <span>Only contacts with copy</span>
        </label>
        <div className="grow" />
        {/* Back to the main page already filtered to this campaign, rather than to
            the whole list with the filtering left as an exercise. */}
        <Link className="ghost sm btn-link" to={`/outreach?campaign=${campaignId}`}>
          Open in Outreach →
        </Link>
      </div>

      {rows.length === 0 ? (
        <div className="empty">
          {all.length === 0
            ? 'No contacts in this campaign yet.'
            : 'Nothing written yet for these contacts. Copy is generated per batch — '
              + 'run /sdr-batches, or generate from the Pipeline view.'}
        </div>
      ) : (
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          {/* tableLayout: fixed — without it the % widths are advisory and long
              signal text stretches its column, which is what made this wonky. */}
          <table className="dense" style={{ tableLayout: 'fixed', width: '100%',
            minWidth: 1080 }}>
            <thead><tr>
              <th style={{ width: '5%' }}>Score</th>
              <th style={{ width: '14%' }}>Name</th>
              <th style={{ width: '12%' }}>Company</th>
              <th style={{ width: '16%' }}>Campaigns</th>
              <th style={{ width: '11%' }}>Offer</th>
              <th style={{ width: '13%' }}>Sequence</th>
              <th style={{ width: '19%' }}>Signal it opens on</th>
              <th style={{ width: '10%' }} />
            </tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.contact_id} className={r.has_copy ? 'clickable' : ''}
                  onClick={() => r.has_copy && setOpenId(r.contact_id)}>
                  <td><ScoreBadge score={r.priority_score} band={r.score_band} /></td>
                  <td><ContactLink contact={{ contact_id: r.contact_id,
                    first_name: r.name.split(' ')[0],
                    last_name: r.name.split(' ').slice(1).join(' ') }} /></td>
                  <td className="muted"><span className="clamp2">{r.company || '—'}</span></td>
                  {/* Which campaigns are working this person. An overlap has to be
                      visible on the row: it is the reason their sequence is merged. */}
                  <td><CampaignTags campaigns={r.campaigns} current={campaignId} /></td>
                  <td>{r.cta_type
                    ? <span className="badge">{r.cta_type}</span>
                    : <span className="muted">—</span>}</td>
                  <td><SequenceCell r={r} /></td>
                  <td className="muted">
                    <span className="clamp2" title={r.signal}>{r.signal || '—'}</span>
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    {r.has_copy
                      ? <span className="badge status-enrolled">written</span>
                      : <span className="badge" title="No copy generated yet">not written</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {openId && <OutreachDetail id={openId} onClose={() => setOpenId(null)} />}
    </div>
  )
}

// Single or merged, and how far through.
//
// A contact in two campaigns does not get two cadences — touch_plan folds every
// campaign's steps into ONE de-conflicted schedule. So "which sequence is this?"
// genuinely has a different answer for them, and the send progress underneath is
// progress against that merged plan rather than against this campaign's four
// emails. Saying "4/4" while eight more touches are queued from elsewhere would be
// the misleading version.
function SequenceCell({ r }) {
  const sq = r.sequence || {}
  const sent = r.seq
  return (
    <div style={{ fontSize: 12, lineHeight: 1.4 }}>
      <span className={'badge ' + (sq.merged ? 'seq-merged' : '')}
        title={sq.merged
          ? `Merged across ${sq.campaigns} campaigns — ${sq.touches} touches over `
            + `${sq.span_days} days, ${sq.conflicts} spaced apart to avoid `
            + 'contacting them twice in a row.'
          : `This campaign's own cadence — ${sq.touches} touches.`}>
        {sq.merged ? `merged · ${sq.touches}` : `single · ${sq.touches}`}
      </span>
      {sent && (
        <div className="muted" style={{ marginTop: 2 }}>
          {sent.email_sent} sent{sent.li_sent ? ` · ${sent.li_sent} li` : ''}
          {sent.replied ? ' · replied' : ''}
        </div>
      )}
    </div>
  )
}
