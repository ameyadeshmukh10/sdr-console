import { motion } from "framer-motion";
import { SlideFrame } from "../../primitives";
import everworkerLogo from "@/assets/everworker-logotype-black.svg";
import { PROSPECT, COVER } from "./bitesData";

// EverWorker ships a dark mark; brightness(0) invert(1) flattens it to pure white.
const WHITE = { filter: "brightness(0) invert(1)" } as const;

/** Cover: EverWorker × Prospect lockup, white on deep emerald, with the GTM tagline. */
const BitesCoverSlide = () => (
  <SlideFrame tone="dark">
    <div aria-hidden className="absolute rounded-full" style={{ width: 560, height: 560, top: -190, left: -150, background: "rgba(82,254,191,0.13)", filter: "blur(100px)" }} />
    <div aria-hidden className="absolute rounded-full" style={{ width: 460, height: 460, bottom: -170, right: -120, background: "rgba(34,130,111,0.20)", filter: "blur(100px)" }} />

    <div className="absolute inset-0 flex flex-col items-center justify-center text-center" style={{ padding: "60px 80px" }}>
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.7, ease: [0.22, 0.61, 0.36, 1] }}
        className="flex items-center justify-center"
        style={{ gap: 44 }}
      >
        <img src={everworkerLogo} alt="EverWorker" style={{ height: 52, width: "auto", ...WHITE }} />
        <span style={{ fontSize: 30, fontWeight: 200, color: "rgba(255,255,255,0.42)", lineHeight: 1 }}>×</span>
        {PROSPECT.logoDataUri ? (
          <img src={PROSPECT.logoDataUri} alt={PROSPECT.name} style={{ height: 56, width: "auto" }} />
        ) : (
          <span style={{ fontSize: 50, fontWeight: 700, letterSpacing: "-0.02em", color: "#FFFFFF", lineHeight: 1, whiteSpace: "nowrap" }}>
            {PROSPECT.logoText}
          </span>
        )}
      </motion.div>

      <motion.p
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.6, delay: 0.45 }}
        style={{ fontSize: 24, fontWeight: 300, letterSpacing: "0.16em", textTransform: "uppercase", whiteSpace: "nowrap", color: "rgba(255,255,255,0.6)", marginTop: 40, paddingLeft: "0.16em" }}
      >
        {COVER.tagline}
      </motion.p>
    </div>
  </SlideFrame>
);

export default BitesCoverSlide;
