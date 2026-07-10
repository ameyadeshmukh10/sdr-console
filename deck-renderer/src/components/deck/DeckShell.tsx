import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Maximize2 } from "lucide-react";

/** Slides are authored against this fixed canvas, then uniformly scaled to fit. */
export const STAGE_W = 1280;
export const STAGE_H = 720;

export type Slide = {
  id: string;
  /** Short label for the presenter dot tooltip / progress. */
  title: string;
  /** Section name for grouping the progress dots and wayfinding. */
  section?: string;
  /** Reference/appendix slides: excluded from the main progress count + dots. */
  appendix?: boolean;
  render: () => React.ReactNode;
};

/**
 * Full-screen 16:9 presentation shell.
 * - Slides are drawn on a 1280x720 canvas and scaled to fit any viewport (letterboxed).
 * - Keyboard: →/Space/PageDn next · ←/PageUp prev · Home/End jump · F fullscreen.
 * - Chrome is a single low-profile bottom bar that reveals on hover/mouse move.
 */
const DeckShell = ({ slides }: { slides: Slide[] }) => {
  const [index, setIndex] = useState(0);
  const [scale, setScale] = useState(1);
  const [chromeVisible, setChromeVisible] = useState(true);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const hideTimer = useRef<number | undefined>(undefined);

  const clamp = useCallback((i: number) => Math.max(0, Math.min(slides.length - 1, i)), [slides.length]);
  const go = useCallback((delta: number) => setIndex((i) => clamp(i + delta)), [clamp]);

  // Fit-to-viewport scaling.
  useLayoutEffect(() => {
    const update = () => {
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      setScale(Math.min(vw / STAGE_W, vh / STAGE_H));
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  // Keyboard navigation.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      switch (e.key) {
        case "ArrowRight":
        case "PageDown":
        case " ":
          e.preventDefault();
          go(1);
          break;
        case "ArrowLeft":
        case "PageUp":
          e.preventDefault();
          go(-1);
          break;
        case "Home":
          e.preventDefault();
          setIndex(0);
          break;
        case "End":
          e.preventDefault();
          setIndex(slides.length - 1);
          break;
        case "f":
        case "F":
          if (!document.fullscreenElement) wrapRef.current?.requestFullscreen?.();
          else document.exitFullscreen?.();
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, slides.length]);

  // Auto-hide chrome when the mouse is idle.
  const wakeChrome = useCallback(() => {
    setChromeVisible(true);
    window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => setChromeVisible(false), 2600);
  }, []);
  useEffect(() => {
    wakeChrome();
    return () => window.clearTimeout(hideTimer.current);
  }, [wakeChrome, index]);

  const slide = slides[index];
  const isAppendix = !!slide.appendix;
  const mainCount = slides.filter((s) => !s.appendix).length;
  const apxCount = slides.length - mainCount;
  const posInMain = slides.slice(0, index + 1).filter((s) => !s.appendix).length;
  const posInApx = slides.slice(0, index + 1).filter((s) => s.appendix).length;

  // Group consecutive MAIN slides by section for the progress-dot clusters.
  // Appendix slides are excluded so the dots + count reflect only the main arc.
  const groups: { section?: string; items: number[] }[] = [];
  slides.forEach((s, i) => {
    if (s.appendix) return;
    const last = groups[groups.length - 1];
    if (last && last.section === s.section) last.items.push(i);
    else groups.push({ section: s.section, items: [i] });
  });

  return (
    <div
      ref={wrapRef}
      onMouseMove={wakeChrome}
      className="fixed inset-0 overflow-hidden flex items-center justify-center"
      style={{ background: "radial-gradient(ellipse 90% 90% at 50% 30%, #0e1714 0%, #080c0b 100%)" }}
    >
      {/* Scaled 16:9 stage */}
      <div
        style={{
          width: STAGE_W,
          height: STAGE_H,
          transform: `scale(${scale})`,
          transformOrigin: "center center",
        }}
        className="relative shrink-0"
      >
        <div className="absolute inset-0 overflow-hidden rounded-[2px]">{slide.render()}</div>
      </div>

      {/* Presenter chrome */}
      <div
        className="pointer-events-none fixed inset-x-0 bottom-0 flex justify-center pb-5 transition-opacity duration-500"
        style={{ opacity: chromeVisible ? 1 : 0 }}
      >
        <div className="pointer-events-auto flex items-center gap-3 rounded-full px-3 py-2 text-white/90 glass-strong"
             style={{ background: "hsla(160 30% 8% / 0.5)", border: "1px solid hsla(0 0% 100% / 0.12)" }}>
          <button
            onClick={() => go(-1)}
            disabled={index === 0}
            aria-label="Previous slide"
            title="Previous (←)"
            className="grid h-8 w-8 place-items-center rounded-full transition-colors hover:bg-white/10 disabled:opacity-30"
          >
            <ChevronLeft size={18} />
          </button>

          <div className="flex items-center gap-2.5 px-1">
            {groups.map((g, gi) => (
              <div key={gi} className="flex items-center gap-1.5" title={g.section}>
                {g.items.map((i) => (
                  <button
                    key={slides[i].id}
                    onClick={() => setIndex(i)}
                    aria-label={`Go to ${slides[i].title}`}
                    title={slides[i].title}
                    className="h-1.5 rounded-full transition-all duration-300"
                    style={{
                      width: i === index ? 22 : 6,
                      background: i === index ? "#52FEBF" : "rgba(255,255,255,0.32)",
                      boxShadow: i === index ? "0 0 10px rgba(82,254,191,0.7)" : "none",
                    }}
                  />
                ))}
              </div>
            ))}
          </div>

          <span className="tabular-nums text-[12px] font-semibold tracking-wide text-white/60 whitespace-nowrap text-center">
            {isAppendix ? (
              <>
                <span className="text-white/45">Appendix · </span>
                {posInApx} / {apxCount}
              </>
            ) : (
              <>
                {slide.section ? <span className="text-white/45">{slide.section} · </span> : null}
                {posInMain} / {mainCount}
              </>
            )}
          </span>

          <button
            onClick={() => go(1)}
            disabled={index === slides.length - 1}
            aria-label="Next slide"
            title="Next (→ / Space)"
            className="grid h-8 w-8 place-items-center rounded-full transition-colors hover:bg-white/10 disabled:opacity-30"
          >
            <ChevronRight size={18} />
          </button>

          <span className="mx-1 h-5 w-px bg-white/15" />

          <button
            onClick={() =>
              !document.fullscreenElement
                ? wrapRef.current?.requestFullscreen?.()
                : document.exitFullscreen?.()
            }
            aria-label="Toggle fullscreen"
            title="Fullscreen (F)"
            className="grid h-8 w-8 place-items-center rounded-full transition-colors hover:bg-white/10"
          >
            <Maximize2 size={15} />
          </button>
        </div>
      </div>
    </div>
  );
};

export default DeckShell;
