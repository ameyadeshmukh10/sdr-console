import DeckShell, { type Slide } from "@/components/deck/DeckShell";
import BitesCoverSlide from "@/components/deck/slides/bites/BitesCoverSlide";
import BitesResearchSlide from "@/components/deck/slides/bites/BitesResearchSlide";
import BitesAccountSlide from "@/components/deck/slides/bites/BitesAccountSlide";
import BitesOutreachSlide from "@/components/deck/slides/bites/BitesOutreachSlide";

/**
 * The AI SDR Playbook — for Bites (mybites.io). Four-slide cut.
 *
 * EverWorker pitching Bites: cover lockup → the research (offering / ICP / buying
 * group) → one worked account (CAVA: signals + resolved real buyers) → the
 * researched outreach itself (email + LinkedIn). The goal: book the meeting.
 */
export const slides: Slide[] = [
  { id: "bites-cover", title: "Cover", section: "Open", render: () => <BitesCoverSlide /> },
  { id: "bites-research", title: "The research", section: "Research", render: () => <BitesResearchSlide /> },
  { id: "bites-account", title: "The play · CAVA", section: "The play", render: () => <BitesAccountSlide /> },
  { id: "bites-outreach", title: "Outreach", section: "The play", render: () => <BitesOutreachSlide /> },
];

const BitesDeckPage = () => <DeckShell slides={slides} />;

export default BitesDeckPage;
