import { Fragment } from "react";
import { HL } from "../playbook/shared";
import { BRAND } from "../../primitives";

/**
 * Turn a plain string carrying ==double-equals== markers into React nodes.
 *
 * Keeps the deck's data layer (deck-data.json) pure text — no JSX — so the
 * research/outreach agents can author copy as strings and still get the
 * deck's emerald highlight treatment.
 *
 *   "Built in ==under 14 minutes=="  →  Built in <emphasis>under 14 minutes</emphasis>
 *
 * mode "pill"  → boxed emerald highlight (used in outreach bodies, matches <HL>)
 * mode "bold"  → inline emerald bold, no background (used in tight bullet lists)
 */
export function renderHighlights(text: string, mode: "pill" | "bold" = "pill"): React.ReactNode {
  // Capturing split → even indices are plain text, odd indices are highlighted.
  const parts = text.split(/==(.+?)==/g);
  return parts.map((part, i) => {
    if (i % 2 === 0) return part ? <Fragment key={i}>{part}</Fragment> : null;
    if (mode === "pill") return <HL key={i}>{part}</HL>;
    return (
      <span key={i} className="font-bold" style={{ color: BRAND.emerald }}>
        {part}
      </span>
    );
  });
}
