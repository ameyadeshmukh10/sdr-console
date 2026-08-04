import { useState } from 'react'
import { useAddons } from './Addon.jsx'
import Addon from './Addon.jsx'

// A contact, everywhere a contact appears: name linked into the CRM, and the phone
// number on demand.
//
// The link matters more than it looks. A prioritised call list whose names are plain
// text means "call this person" is really "copy a name into another tab and find
// them" — which is where a call list stops being used. The contact id in this console
// IS the CRM record id, so the only missing piece was the portal.
//
// Phone is part of the Scale package. It is shown behind one click rather than
// printed in every row: a table of phone numbers is a table nobody can screenshot,
// and the reveal is also the natural place to mark the tier.

export function contactUrl(crm, id) {
  if (!crm?.available || !crm.contact_url || !id) return null
  return crm.contact_url.replace('{id}', encodeURIComponent(id))
}

export default function ContactLink({ contact, showTitle = false }) {
  const { crm } = useAddons()
  const name = `${contact.first_name || ''} ${contact.last_name || ''}`.trim()
    || contact.name || contact.contact_id
  const href = contactUrl(crm, contact.contact_id)
  return (
    <span className="clink">
      {href ? (
        <a href={href} target="_blank" rel="noreferrer"
          title="Open in HubSpot">
          {name}
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 4h6v6" /><path d="M20 4 10 14" />
            <path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
          </svg>
        </a>
      ) : name}
      {showTitle && contact.title && (
        <span className="clink-t" title={contact.title}>{contact.title}</span>
      )}
    </span>
  )
}

// Reveal-on-click, because a grid of phone numbers is not something you want on a
// shared screen — and the click is where the tier mark belongs.
export function Phone({ contact, withBadge = false }) {
  const [shown, setShown] = useState(false)
  const number = contact.mobile_phone || contact.phone
  if (!number) return <span className="muted">—</span>
  if (!shown) {
    return (
      <button type="button" className="phone-reveal" onClick={() => setShown(true)}
        title="Show phone number">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
          <path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.2a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z" />
        </svg>
        Show
        {withBadge && <Addon id="phone-reveal" />}
      </button>
    )
  }
  return <a className="phone" href={`tel:${number.replace(/[^\d+]/g, '')}`}>{number}</a>
}
