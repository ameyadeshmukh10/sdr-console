import { motion } from "framer-motion";
import { Check } from "lucide-react";
import { Kicker, BRAND } from "../../primitives";

/**
 * Shared building blocks for the AI SDR Playbook deck's four-beat plays.
 *
 * Generalized from the inline Nomology slides in WebDeanonDeckPage.tsx
 * (CustomerSlide / ResolveSlide / EnrichSlide / SequenceSlide) so every play
 * — Nomology, Testkube, Usercentrics, Memgraph — renders with an identical rhythm,
 * driven only by data.
 */

export { BRAND };

export const EASE: [number, number, number, number] = [0.22, 0.61, 0.36, 1];
export const LI_BLUE = "#0A66C2";

export const CARD: React.CSSProperties = {
  background: "linear-gradient(180deg, #FFFFFF 0%, rgba(255,255,255,0.94) 100%)",
  border: "1px solid rgba(15,28,24,0.08)",
  boxShadow: "0 30px 70px -34px rgba(15,40,32,0.4)",
};
export const CARD_SOFT: React.CSSProperties = {
  background: "linear-gradient(180deg, #FFFFFF 0%, rgba(255,255,255,0.92) 100%)",
  border: "1px solid rgba(15,28,24,0.08)",
  boxShadow: "0 22px 54px -36px rgba(15,40,32,0.38)",
};

/** Emerald highlight span for signal phrases inside outreach copy. */
export const HL = ({ children }: { children: React.ReactNode }) => (
  <span style={{ background: "rgba(34,130,111,0.14)", color: BRAND.emerald, borderRadius: 4, padding: "0 4px", fontWeight: 700 }}>
    {children}
  </span>
);

/** Centered slide header: kicker (or custom eyebrow) + lead/bold headline + optional sub. */
export const Header = ({
  kicker,
  eyebrow,
  lead,
  bold,
  sub,
}: {
  kicker?: string;
  /** Custom eyebrow node — overrides the plain `kicker` string (e.g. a play-identity chip). */
  eyebrow?: React.ReactNode;
  lead: string;
  bold: string;
  sub?: string;
}) => (
  <div className="flex flex-col items-center text-center shrink-0">
    {eyebrow ?? (kicker ? <Kicker delay={0.05}>{kicker}</Kicker> : null)}
    <motion.h1
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.6, delay: 0.12, ease: EASE }}
      className="font-medium tracking-tight"
      style={{ fontSize: 46, lineHeight: 1.05, color: BRAND.ink, marginTop: 14 }}
    >
      {lead} <span className="font-extrabold">{bold}</span>
    </motion.h1>
    {sub && (
      <motion.p
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.5, delay: 0.24 }}
        className="font-medium tracking-tight"
        style={{ fontSize: 16.5, lineHeight: 1.5, color: "rgba(15,28,24,0.6)", marginTop: 13, maxWidth: 760 }}
      >
        {sub}
      </motion.p>
    )}
  </div>
);

/** Wordmark chip — tech stack, signal correlation, hiring roles, usage facts. */
export const Chip = ({ children }: { children: React.ReactNode }) => (
  <span
    className="rounded-full font-semibold tracking-tight"
    style={{ fontSize: 12.5, padding: "6px 12px", background: "rgba(34,130,111,0.08)", border: "1px solid rgba(34,130,111,0.18)", color: BRAND.ink }}
  >
    {children}
  </span>
);

/** Small verified-data badge (email / mobile). */
export const VerifyBadge = ({ label }: { label: string }) => (
  <span className="flex items-center gap-1 rounded-md" style={{ padding: "2px 7px", background: "rgba(34,130,111,0.1)", border: "1px solid rgba(34,130,111,0.2)" }}>
    <Check size={10} color={BRAND.emerald} strokeWidth={3} />
    <span className="font-semibold tracking-tight" style={{ fontSize: 10.5, color: BRAND.emerald }}>{label}</span>
  </span>
);

export type Contact = { initials: string; name: string; title: string; mobile?: boolean };

/** Buying-group contact row. */
export const ContactRow = ({ initials, name, title, index, mobile }: Contact & { index: number }) => (
  <motion.div
    initial={{ opacity: 0, x: 12 }}
    animate={{ opacity: 1, x: 0 }}
    transition={{ duration: 0.4, delay: 0.6 + index * 0.1, ease: EASE }}
    className="flex items-center gap-3"
    style={{ padding: "10px 18px", borderBottom: "1px solid rgba(15,28,24,0.06)" }}
  >
    <span className="grid place-items-center rounded-lg font-bold shrink-0" style={{ width: 34, height: 34, fontSize: 12, color: "#0F1C18", background: "linear-gradient(135deg, #22826F, #52FEBF)", boxShadow: "0 0 14px rgba(82,254,191,0.35)" }}>
      {initials}
    </span>
    <div className="min-w-0 flex-1">
      <p className="font-bold tracking-tight leading-tight truncate" style={{ fontSize: 13.5, color: BRAND.ink }}>{name}</p>
      <p className="font-medium tracking-tight leading-tight truncate" style={{ fontSize: 11.5, color: "rgba(15,28,24,0.55)", marginTop: 1 }}>{title}</p>
    </div>
    <div className="flex items-center gap-1.5 shrink-0">
      <VerifyBadge label="email" />
      {mobile && <VerifyBadge label="mobile" />}
    </div>
  </motion.div>
);
