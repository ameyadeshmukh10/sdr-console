import { motion } from "framer-motion";

export const BRAND = {
  emerald: "#22826F",
  mint: "#52FEBF",
  ink: "#0F1C18",
};

/**
 * Full-stage slide background. `tone="light"` is the warm brand mesh (default);
 * `tone="dark"` is the deep emerald panel used for impact/closing slides.
 */
export const SlideFrame = ({
  children,
  tone = "light",
  className = "",
}: {
  children: React.ReactNode;
  tone?: "light" | "dark";
  className?: string;
}) => {
  const bg =
    tone === "dark"
      ? "radial-gradient(ellipse 70% 90% at 50% -10%, rgba(82,254,191,0.28) 0%, transparent 55%), linear-gradient(180deg, #0F1C18 0%, #16241f 100%)"
      : `radial-gradient(ellipse 60% 50% at 18% 22%, hsla(164 60% 32% / 0.10) 0%, transparent 70%),
         radial-gradient(ellipse 55% 60% at 85% 78%, hsla(210 60% 50% / 0.07) 0%, transparent 70%),
         radial-gradient(ellipse 70% 45% at 55% 8%, hsla(45 60% 60% / 0.06) 0%, transparent 70%),
         linear-gradient(180deg, #FBFCFB 0%, #F3F6F4 100%)`;
  return (
    <div className={`absolute inset-0 ${className}`} style={{ background: bg }}>
      {/* faint grain for depth */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 mix-blend-multiply"
        style={{
          opacity: tone === "dark" ? 0.18 : 0.22,
          mixBlendMode: tone === "dark" ? "overlay" : "multiply",
          backgroundImage:
            "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0.14 0'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>\")",
        }}
      />
      {children}
    </div>
  );
};

/** Small tracked uppercase eyebrow above a headline. */
export const Kicker = ({
  children,
  tone = "light",
  delay = 0,
}: {
  children: React.ReactNode;
  tone?: "light" | "dark";
  delay?: number;
}) => (
  <motion.p
    initial={{ opacity: 0, y: 8 }}
    animate={{ opacity: 1, y: 0 }}
    transition={{ duration: 0.5, delay }}
    className="text-[15px] font-semibold uppercase"
    style={{
      letterSpacing: "0.26em",
      color: tone === "dark" ? "#52FEBF" : BRAND.emerald,
    }}
  >
    {children}
  </motion.p>
);
