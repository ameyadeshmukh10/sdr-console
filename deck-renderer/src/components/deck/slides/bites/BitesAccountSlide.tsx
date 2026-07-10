import { motion } from "framer-motion";
import { TrendingUp, Briefcase, Radar, Sparkles, ShieldCheck, Mail, Check, Users, Newspaper, type LucideIcon } from "lucide-react";
import { FaLinkedin } from "react-icons/fa";
import { SlideFrame, BRAND } from "../../primitives";
import { CARD, EASE, LI_BLUE } from "../playbook/shared";
import { ACCOUNT, SIGNALS, CONTACTS, type ResolvedContact } from "./bitesData";

const SIGNAL_ICON: Record<string, LucideIcon> = { expansion: TrendingUp, hiring: Briefcase, tech: Radar, program: Sparkles, news: Newspaper };

const ContactCard = ({ c, delay }: { c: ResolvedContact; delay: number }) => (
  <motion.div
    initial={{ opacity: 0, x: 12 }}
    animate={{ opacity: 1, x: 0 }}
    transition={{ duration: 0.45, delay, ease: EASE }}
    className="rounded-xl"
    style={{ padding: "15px 16px", background: "#FAFBFB", border: "1px solid rgba(15,28,24,0.08)" }}
  >
    <div className="flex items-center" style={{ gap: 12 }}>
      <span className="grid place-items-center rounded-lg font-bold shrink-0" style={{ width: 42, height: 42, fontSize: 14, color: "#0F1C18", background: "linear-gradient(135deg, #22826F, #52FEBF)", boxShadow: "0 0 14px rgba(82,254,191,0.35)" }}>{c.initials}</span>
      <div className="min-w-0 flex-1">
        <p className="font-bold tracking-tight leading-tight" style={{ fontSize: 15, color: BRAND.ink }}>{c.name}</p>
        <p className="font-medium tracking-tight leading-tight" style={{ fontSize: 11.5, color: "rgba(15,28,24,0.55)", marginTop: 2 }}>{c.title}</p>
      </div>
    </div>
    <div className="flex flex-col" style={{ gap: 7, marginTop: 12 }}>
      <div className="flex items-center" style={{ gap: 8 }}>
        <Mail size={13} color={BRAND.emerald} strokeWidth={2.2} className="shrink-0" />
        <span className="font-semibold tracking-tight truncate" style={{ fontSize: 12, color: BRAND.ink }}>{c.email}</span>
        <span className="flex items-center gap-1 rounded shrink-0" style={{ padding: "1px 5px", background: "rgba(34,130,111,0.1)" }}>
          <Check size={8} color={BRAND.emerald} strokeWidth={3} /><span className="font-semibold" style={{ fontSize: 8.5, color: BRAND.emerald }}>verified</span>
        </span>
      </div>
      <div className="flex items-center" style={{ gap: 8 }}>
        <FaLinkedin size={13} color={LI_BLUE} className="shrink-0" />
        <span className="font-semibold tracking-tight truncate" style={{ fontSize: 12, color: LI_BLUE }}>{c.linkedin}</span>
      </div>
    </div>
  </motion.div>
);

const BitesAccountSlide = () => (
  <SlideFrame>
    <div className="absolute inset-0 flex flex-col" style={{ padding: "40px 52px 36px" }}>
      <div className="flex flex-col items-center text-center shrink-0">
        <motion.h1
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.12, ease: EASE }}
          className="font-medium tracking-tight"
          style={{ fontSize: 40, lineHeight: 1.04, color: BRAND.ink }}
        >
          Deeply researches <span className="font-extrabold">the company and buying group.</span>
        </motion.h1>
      </div>

      <div className="flex-1 flex items-center" style={{ marginTop: 20 }}>
        <div className="flex items-stretch w-full" style={{ gap: 18 }}>
        {/* Account */}
        <motion.div
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.26, ease: EASE }}
          className="rounded-2xl flex flex-col overflow-hidden"
          style={{ ...CARD, flex: "0 0 340px" }}
        >
          <div className="flex items-center" style={{ gap: 11, padding: "12px 18px", borderBottom: "1px solid rgba(15,28,24,0.07)", background: "#FAFBFB" }}>
            <span className="grid place-items-center rounded-lg font-bold shrink-0" style={{ width: 40, height: 40, fontSize: 15, color: "#0F1C18", background: "linear-gradient(135deg, #22826F, #52FEBF)", boxShadow: "0 0 14px rgba(82,254,191,0.35)" }}>{ACCOUNT.name.charAt(0)}</span>
            <div className="min-w-0">
              <div className="flex items-center" style={{ gap: 7 }}>
                <p className="font-bold tracking-tight" style={{ fontSize: 16, color: BRAND.ink }}>{ACCOUNT.name}</p>
                {ACCOUNT.ticker && <span className="rounded font-semibold tracking-tight" style={{ fontSize: 9, padding: "2px 6px", background: "rgba(34,130,111,0.1)", border: "1px solid rgba(34,130,111,0.2)", color: BRAND.emerald }}>{ACCOUNT.ticker}</span>}
              </div>
              <p className="font-medium tracking-tight truncate" style={{ fontSize: 11, color: "rgba(15,28,24,0.55)", marginTop: 2 }}>{ACCOUNT.blurb}</p>
            </div>
          </div>

          <div className="flex-1 flex flex-col justify-center" style={{ padding: "18px 18px", gap: 16 }}>
            <div className="grid" style={{ gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
              {ACCOUNT.stats.map((s) => (
                <div key={s.label} className="rounded-lg" style={{ padding: "11px 10px", background: "rgba(34,130,111,0.06)", border: "1px solid rgba(34,130,111,0.12)" }}>
                  <p className="font-extrabold tracking-tight" style={{ fontSize: 19, color: BRAND.emerald, lineHeight: 1 }}>{s.value}</p>
                  <p className="font-medium tracking-tight" style={{ fontSize: 9.5, color: "rgba(15,28,24,0.6)", marginTop: 5, lineHeight: 1.2 }}>{s.label}</p>
                </div>
              ))}
            </div>

            <div className="rounded-xl" style={{ padding: "13px 14px", background: "rgba(34,130,111,0.05)", border: "1px dashed rgba(34,130,111,0.25)" }}>
              <div className="flex items-center" style={{ gap: 7, marginBottom: 6 }}>
                <ShieldCheck size={13} color={BRAND.emerald} strokeWidth={2.4} />
                <span className="font-bold uppercase tracking-tight" style={{ fontSize: 9, letterSpacing: "0.14em", color: BRAND.emerald }}>Why it cleared the ICP gate</span>
              </div>
              <p className="font-medium tracking-tight" style={{ fontSize: 11.5, lineHeight: 1.45, color: "rgba(15,28,24,0.66)" }}>{ACCOUNT.gate}</p>
            </div>
          </div>
        </motion.div>

        {/* Signal stack */}
        <motion.div
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.38, ease: EASE }}
          className="rounded-2xl flex flex-col flex-1 overflow-hidden"
          style={CARD}
        >
          <div className="flex items-center" style={{ gap: 9, padding: "12px 18px", borderBottom: "1px solid rgba(15,28,24,0.07)", background: "#FAFBFB" }}>
            <Radar size={15} color={BRAND.emerald} strokeWidth={2.2} />
            <span className="font-bold tracking-tight" style={{ fontSize: 13.5, color: BRAND.ink }}>Signal intelligence</span>
          </div>
          <div className="flex-1 flex flex-col justify-center" style={{ padding: "4px 18px" }}>
            {SIGNALS.map((s, i) => {
              const Icon = SIGNAL_ICON[s.kind] || Radar;
              return (
                <motion.div
                  key={s.label}
                  initial={{ opacity: 0, x: 12 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ duration: 0.4, delay: 0.5 + i * 0.08, ease: EASE }}
                  className="flex items-start"
                  style={{ gap: 11, padding: "9px 0", borderBottom: i < SIGNALS.length - 1 ? "1px solid rgba(15,28,24,0.06)" : "none" }}
                >
                  <span className="grid place-items-center rounded-lg shrink-0" style={{ width: 28, height: 28, background: "rgba(34,130,111,0.1)", marginTop: 1 }}>
                    <Icon size={14} color={BRAND.emerald} strokeWidth={2.2} />
                  </span>
                  <div className="min-w-0">
                    <p className="font-bold tracking-tight" style={{ fontSize: 12.5, color: BRAND.ink }}>{s.label}</p>
                    <p className="font-medium tracking-tight" style={{ fontSize: 11.5, lineHeight: 1.35, color: "rgba(15,28,24,0.62)", marginTop: 1 }}>{s.detail}</p>
                  </div>
                </motion.div>
              );
            })}
          </div>
        </motion.div>

        {/* Buying group */}
        <motion.div
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.5, ease: EASE }}
          className="rounded-2xl flex flex-col overflow-hidden"
          style={{ ...CARD, flex: "0 0 312px" }}
        >
          <div className="flex items-center" style={{ gap: 9, padding: "12px 18px", borderBottom: "1px solid rgba(15,28,24,0.07)", background: "#FAFBFB" }}>
            <Users size={15} color={BRAND.emerald} strokeWidth={2.2} />
            <span className="font-bold tracking-tight" style={{ fontSize: 13.5, color: BRAND.ink }}>Buying group, resolved</span>
          </div>
          <div className="flex-1 flex flex-col justify-center" style={{ padding: "16px 16px", gap: 12 }}>
            {CONTACTS.map((c, i) => <ContactCard key={c.email} c={c} delay={0.6 + i * 0.1} />)}
          </div>
        </motion.div>
        </div>
      </div>
    </div>
  </SlideFrame>
);

export default BitesAccountSlide;
