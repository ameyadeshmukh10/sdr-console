import { motion } from "framer-motion";
import { Video, Target, Users, Check, Sparkles, type LucideIcon } from "lucide-react";
import { SlideFrame, BRAND } from "../../primitives";
import { CARD, EASE } from "../playbook/shared";
import { OFFERING, ICP, BUYING_GROUP } from "./bitesData";
import { renderHighlights } from "./highlight";

const Pill = ({ children }: { children: React.ReactNode }) => (
  <span className="rounded-full font-semibold tracking-tight" style={{ fontSize: 11.5, padding: "4px 10px", background: "rgba(34,130,111,0.08)", border: "1px solid rgba(34,130,111,0.18)", color: BRAND.ink }}>{children}</span>
);

const ColHead = ({ icon: Icon, label }: { icon: LucideIcon; label: string }) => (
  <div className="flex items-center" style={{ gap: 11 }}>
    <span className="grid place-items-center rounded-xl shrink-0" style={{ width: 38, height: 38, background: "linear-gradient(135deg, rgba(34,130,111,0.16), rgba(82,254,191,0.12))", border: "1px solid rgba(34,130,111,0.2)" }}>
      <Icon size={19} color={BRAND.emerald} strokeWidth={2.2} />
    </span>
    <p className="font-extrabold tracking-tight" style={{ fontSize: 18, color: BRAND.ink }}>{label}</p>
  </div>
);

const SectionLabel = ({ children }: { children: React.ReactNode }) => (
  <p className="font-bold uppercase tracking-tight" style={{ fontSize: 9.5, letterSpacing: "0.15em", color: "rgba(15,28,24,0.4)", marginBottom: 9 }}>{children}</p>
);

const Bullet = ({ children }: { children: React.ReactNode }) => (
  <div className="flex items-start" style={{ gap: 9 }}>
    <span className="grid place-items-center rounded-full shrink-0" style={{ width: 16, height: 16, background: "rgba(34,130,111,0.12)", marginTop: 2 }}>
      <Check size={10} color={BRAND.emerald} strokeWidth={3} />
    </span>
    <span className="font-medium tracking-tight" style={{ fontSize: 13, lineHeight: 1.35, color: "rgba(15,28,24,0.78)" }}>{children}</span>
  </div>
);

const divider = { height: 1, background: "rgba(15,28,24,0.08)", margin: "18px 0" } as const;

const FEATURED: React.CSSProperties = {
  background: "linear-gradient(165deg, rgba(34,130,111,0.10) 0%, rgba(82,254,191,0.05) 100%)",
  border: "1px solid rgba(34,130,111,0.28)",
  boxShadow: "0 30px 70px -34px rgba(15,40,32,0.4)",
};

const Column = ({ delay, children, featured = false }: { delay: number; children: React.ReactNode; featured?: boolean }) => (
  <motion.div
    initial={{ opacity: 0, y: 16 }}
    animate={{ opacity: 1, y: 0 }}
    transition={{ duration: 0.5, delay, ease: EASE }}
    className="rounded-2xl flex flex-col"
    style={{ ...(featured ? FEATURED : CARD), padding: "24px 24px" }}
  >
    {children}
  </motion.div>
);

const BitesResearchSlide = () => (
  <SlideFrame>
    <div className="absolute inset-0 flex flex-col" style={{ padding: "46px 56px 42px" }}>
      <div className="flex flex-col items-center text-center shrink-0">
        <motion.h1
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.1, ease: EASE }}
          className="font-medium tracking-tight"
          style={{ fontSize: 40, lineHeight: 1.04, color: BRAND.ink }}
        >
          Knows your <span className="font-extrabold">business context.</span>
        </motion.h1>
      </div>

      <div className="flex-1 flex items-center" style={{ marginTop: 24 }}>
        <div className="grid w-full items-stretch" style={{ gridTemplateColumns: "1.25fr 1fr 1fr", gap: 20 }}>
          {/* Offering — featured (the product) */}
          <Column delay={0.26} featured>
            <ColHead icon={Video} label="Offering" />
            <div style={divider} />
            <p className="font-bold tracking-tight" style={{ fontSize: 16, lineHeight: 1.3, color: BRAND.ink }}>
              {OFFERING.headline}
            </p>
            <div className="flex flex-col" style={{ gap: 9, marginTop: 14 }}>
              {OFFERING.bullets.map((b, i) => <Bullet key={i}>{renderHighlights(b, "bold")}</Bullet>)}
            </div>

            <div className="rounded-xl" style={{ marginTop: "auto", padding: "14px 15px", background: "#FFFFFF", border: "1px solid rgba(15,28,24,0.08)", boxShadow: "0 14px 30px -24px rgba(15,40,32,0.3)" }}>
              <div className="flex items-center justify-between">
                <div className="flex items-center" style={{ gap: 8 }}>
                  <span className="grid place-items-center rounded-lg shrink-0" style={{ width: 24, height: 24, background: "linear-gradient(135deg, #52FEBF, #22826F)", boxShadow: "0 0 12px rgba(82,254,191,0.4)" }}>
                    <Sparkles size={13} color="#0F1C18" strokeWidth={2.2} />
                  </span>
                  <p className="font-bold tracking-tight" style={{ fontSize: 14, color: BRAND.ink }}>{OFFERING.core.name}</p>
                </div>
                <span className="font-bold uppercase tracking-tight" style={{ fontSize: 8, letterSpacing: "0.14em", color: BRAND.emerald }}>Core product</span>
              </div>
              <p className="font-medium tracking-tight" style={{ fontSize: 11.5, lineHeight: 1.45, color: "rgba(15,28,24,0.66)", marginTop: 9 }}>
                {OFFERING.core.desc}
              </p>
              <div className="flex flex-wrap" style={{ gap: 6, marginTop: 10 }}>
                {OFFERING.core.tags.map((t) => <Pill key={t}>{t}</Pill>)}
              </div>
              <p className="font-medium tracking-tight" style={{ fontSize: 11, color: "rgba(15,28,24,0.5)", marginTop: 11 }}>
                Trusted by <span className="font-bold" style={{ color: BRAND.ink }}>{OFFERING.core.trustedBy}</span>
              </p>
            </div>
          </Column>

          {/* ICP */}
          <Column delay={0.38}>
            <ColHead icon={Target} label="ICP" />
            <div style={divider} />
            <p className="font-bold tracking-tight" style={{ fontSize: 15, lineHeight: 1.4, color: BRAND.ink }}>
              {ICP.summary}
            </p>
            <div style={{ marginTop: 18 }}>
              <SectionLabel>Size</SectionLabel>
              <p className="font-semibold tracking-tight" style={{ fontSize: 13, color: "rgba(15,28,24,0.8)" }}>{ICP.size}</p>
            </div>
            <div style={{ marginTop: 18 }}>
              <SectionLabel>Industries</SectionLabel>
              <div className="flex flex-wrap" style={{ gap: 6 }}>
                {ICP.industries.map((i) => <Pill key={i}>{i}</Pill>)}
              </div>
            </div>
            <div style={{ marginTop: 18 }}>
              <SectionLabel>Why now</SectionLabel>
              <div className="flex flex-col" style={{ gap: 8 }}>
                {ICP.whyNow.map((w, i) => <Bullet key={i}>{w}</Bullet>)}
              </div>
            </div>
          </Column>

          {/* Buying Group */}
          <Column delay={0.5}>
            <ColHead icon={Users} label="Buying Group" />
            <div style={divider} />
            <div className="flex-1 flex flex-col">
              {BUYING_GROUP.map((r, i) => (
                <div key={r.tier} className="flex-1 flex flex-col justify-center" style={{ borderBottom: i < BUYING_GROUP.length - 1 ? "1px solid rgba(15,28,24,0.07)" : "none" }}>
                  <div className="flex items-baseline" style={{ gap: 8 }}>
                    <span className="font-extrabold tracking-tight" style={{ fontSize: 15, color: BRAND.ink }}>{r.tier}</span>
                    <span className="font-medium tracking-tight" style={{ fontSize: 11, color: BRAND.emerald }}>{r.owns}</span>
                  </div>
                  <p className="font-medium tracking-tight" style={{ fontSize: 12.5, lineHeight: 1.4, color: "rgba(15,28,24,0.62)", marginTop: 4 }}>{r.who}</p>
                </div>
              ))}
            </div>
          </Column>
        </div>
      </div>
    </div>
  </SlideFrame>
);

export default BitesResearchSlide;
