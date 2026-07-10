import { Fragment } from "react";
import { motion } from "framer-motion";
import { Mail } from "lucide-react";
import { FaLinkedin } from "react-icons/fa";
import { SlideFrame, BRAND } from "../../primitives";
import { CARD, EASE, LI_BLUE } from "../playbook/shared";
import { EMAIL_DRAFTS, LINKEDIN_DRAFTS, type EmailDraft, type LinkedInDraft } from "./bitesData";

const LinkedInCard = ({ d, delay }: { d: LinkedInDraft; delay: number }) => (
  <motion.div
    initial={{ opacity: 0, y: 14 }}
    animate={{ opacity: 1, y: 0 }}
    transition={{ duration: 0.5, delay, ease: EASE }}
    className="rounded-2xl flex flex-col overflow-hidden h-full"
    style={CARD}
  >
    <div className="flex items-center" style={{ gap: 12, padding: "12px 18px", borderBottom: "1px solid rgba(15,28,24,0.07)", background: "#FAFBFB" }}>
      <span className="grid place-items-center rounded-full font-bold shrink-0" style={{ width: 38, height: 38, fontSize: 13, color: "#0F1C18", background: "linear-gradient(135deg, #22826F, #52FEBF)", boxShadow: "0 0 12px rgba(82,254,191,0.3)" }}>{d.initials}</span>
      <div className="min-w-0 flex-1">
        <p className="font-bold tracking-tight leading-tight truncate" style={{ fontSize: 14, color: BRAND.ink }}>{d.to}</p>
        <p className="font-medium tracking-tight leading-tight truncate" style={{ fontSize: 11, color: "rgba(15,28,24,0.5)", marginTop: 1 }}>{d.title}</p>
      </div>
      <FaLinkedin size={18} color={LI_BLUE} className="shrink-0" />
    </div>
    <div className="flex-1 flex" style={{ padding: "16px 18px" }}>
      <div className="rounded-2xl rounded-tl-md self-start" style={{ padding: "12px 15px", background: "#F1F4F8", border: "1px solid rgba(10,102,194,0.1)" }}>
        <p className="font-medium" style={{ fontSize: 13, lineHeight: 1.55, color: BRAND.ink }}>{d.body}</p>
      </div>
    </div>
  </motion.div>
);

const EmailCard = ({ d, delay }: { d: EmailDraft; delay: number }) => (
  <motion.div
    initial={{ opacity: 0, y: 14 }}
    animate={{ opacity: 1, y: 0 }}
    transition={{ duration: 0.5, delay, ease: EASE }}
    className="rounded-2xl flex flex-col overflow-hidden h-full"
    style={CARD}
  >
    <div className="flex items-center" style={{ gap: 10, padding: "12px 18px", borderBottom: "1px solid rgba(15,28,24,0.07)", background: "#FAFBFB" }}>
      <span className="grid place-items-center rounded-md shrink-0" style={{ width: 26, height: 26, background: "rgba(34,130,111,0.1)" }}>
        <Mail size={14} color={BRAND.emerald} strokeWidth={2.2} />
      </span>
      <span className="font-semibold tracking-tight" style={{ fontSize: 11.5, color: "rgba(15,28,24,0.42)" }}>To</span>
      <span className="font-bold tracking-tight truncate" style={{ fontSize: 12.5, color: BRAND.ink }}>{d.address}</span>
    </div>
    <div className="flex-1" style={{ padding: "15px 18px" }}>
      <p className="font-bold tracking-tight" style={{ fontSize: 14, color: BRAND.ink, marginBottom: 10 }}>{d.subject}</p>
      <p className="font-medium" style={{ fontSize: 13, lineHeight: 1.55, color: BRAND.ink }}>{d.body}</p>
    </div>
  </motion.div>
);

const ColHeader = ({ icon, label, delay }: { icon: React.ReactNode; label: string; delay: number }) => (
  <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.4, delay }} className="flex items-center" style={{ gap: 9 }}>
    {icon}
    <span className="font-bold uppercase tracking-tight" style={{ fontSize: 11, letterSpacing: "0.16em", color: "rgba(15,28,24,0.5)" }}>{label}</span>
  </motion.div>
);

const PEOPLE = [0, 1];

const BitesOutreachSlide = () => (
  <SlideFrame>
    <div className="absolute inset-0 flex flex-col" style={{ padding: "40px 56px 40px" }}>
      <div className="flex flex-col items-center text-center shrink-0">
        <motion.h1
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.12, ease: EASE }}
          className="font-medium tracking-tight"
          style={{ fontSize: 40, lineHeight: 1.04, color: BRAND.ink }}
        >
          Engages the buying group <span className="font-extrabold">on multiple channels.</span>
        </motion.h1>
      </div>

      <div className="flex-1 flex flex-col justify-center" style={{ marginTop: 22 }}>
        <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", columnGap: 26, marginBottom: 13 }}>
          <ColHeader icon={<FaLinkedin size={15} color={LI_BLUE} />} label="LinkedIn" delay={0.24} />
          <ColHeader icon={<Mail size={15} color={BRAND.emerald} strokeWidth={2.3} />} label="Email" delay={0.28} />
        </div>
        <div className="grid items-stretch" style={{ gridTemplateColumns: "1fr 1fr", columnGap: 26, rowGap: 18 }}>
          {PEOPLE.map((i) => (
            <Fragment key={i}>
              <LinkedInCard d={LINKEDIN_DRAFTS[i]} delay={0.32 + i * 0.12} />
              <EmailCard d={EMAIL_DRAFTS[i]} delay={0.38 + i * 0.12} />
            </Fragment>
          ))}
        </div>
      </div>
    </div>
  </SlideFrame>
);

export default BitesOutreachSlide;
