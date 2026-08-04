import { createContext, useContext, useEffect, useState } from 'react'
import { api } from '../api.js'

// Tier marks for the separately-sold agents and add-ons.
//
// The console reads as one product, which is right for using it and wrong for
// selling it: several of the most valuable pieces are individually-priced agents and
// nothing said so.
//
// Deliberately understated. A badge that shouts turns the console into a pricing
// page and gets tuned out by the people in it daily; a badge that is merely PRESENT
// is something a rep can point at mid-demo. So: a small pill beside the feature's
// own heading, detail in the tooltip. No modals, no locks, no upsell interstitials —
// nothing here gates anything.

const Ctx = createContext({ addons: {}, tiers: {}, credits: 15000, customNote: '', crm: {} })

export function AddonProvider({ children }) {
  const [v, setV] = useState({ addons: {}, tiers: {}, credits: 15000, customNote: '', crm: {} })
  useEffect(() => {
    api.tiers().then((d) => setV({
      addons: Object.fromEntries((d.addons || []).map((a) => [a.id, a])),
      tiers: d.tiers || {},
      credits: d.advanced_credits_per_month ?? 15000,
      customNote: d.custom_setup_note || '',
      // CRM deep-link config rides along: both are app-wide, fetched once.
      crm: d.crm || {},
    })).catch(() => {})
  }, [])
  return <Ctx.Provider value={v}>{children}</Ctx.Provider>
}

export function useAddons() { return useContext(Ctx) }

export default function Addon({ id }) {
  const { addons, tiers, customNote } = useAddons()
  const a = addons[id]
  if (!a) return null
  const t = tiers[a.tier] || {}
  const title = `${a.name} — ${t.label || a.tier}. ${a.what}`
    + (a.custom_setup ? ` ${customNote}` : '')
  return (
    <>
      <span className={`addon addon-${a.tier}`} title={title}>{t.label || a.tier}</span>
      {/* Custom setup is a property of some Advanced items, not a tier of its own —
          it rides alongside so a rep can say "Advanced, and it's a custom setup". */}
      {a.custom_setup && (
        <span className="addon addon-custom" title={title}>Custom setup</span>
      )}
    </>
  )
}

// Heading + tier mark, for the many places that pair the two.
export function AddonTitle({ id, children, as: As = 'h2', className = 'section-h' }) {
  return (
    <As className={className} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      {children}<Addon id={id} />
    </As>
  )
}
