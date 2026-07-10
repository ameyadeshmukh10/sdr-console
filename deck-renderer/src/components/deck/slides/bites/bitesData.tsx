import deckData from "../../../../../deck-data.json";
import { renderHighlights } from "./highlight";

/**
 * Bites AI SDR Playbook — typed view over deck-data.json.
 *
 * deck-data.json is the SINGLE fill file. Agent 3 overwrites it with the
 * tailored facts (prospect business + a discovered target account) and rebuilds.
 * This module just types the raw JSON and turns ==marked== copy strings into the
 * deck's highlight nodes, so the four slide components stay 100% data-driven and
 * nothing variable is hardcoded in JSX anymore.
 *
 * Two companies, never conflated:
 *   prospect / cover / research  → the company the deck is FOR (EverWorker's buyer)
 *   target / outreach            → an account that fits the prospect's ICP, prospected into
 */

/* ─────────────────────────────  RAW JSON TYPES  ───────────────────────────── */

type RawData = {
  prospect: { name: string; logoText?: string; logoDataUri?: string };
  cover: { tagline: string };
  research: {
    offering: {
      headline: string;
      bullets: string[];
      core: { name: string; desc: string; tags: string[]; trustedBy: string };
    };
    icp: { summary: string; size: string; industries: string[]; whyNow: string[] };
    buyingGroup: { tier: string; owns: string; who: string }[];
  };
  target: {
    name: string;
    ticker?: string;
    blurb: string;
    stats: { value: string; label: string }[];
    gate: string;
    signals: { kind: SignalKind; label: string; detail: string }[];
    contacts: RawContact[];
  };
  outreach: {
    linkedin: { to: string; title: string; initials: string; body: string }[];
    email: { to: string; address: string; subject: string; body: string }[];
  };
};

type RawContact = {
  initials: string;
  name: string;
  title: string;
  meta?: string;
  tag?: string;
  email: string;
  linkedin: string;
  linkedinUrl: string;
};

export type SignalKind = "expansion" | "hiring" | "tech" | "program" | "news";

const data = deckData as RawData;

/* ─────────────────────────────  COVER + PROSPECT  ───────────────────────────── */

export const PROSPECT = {
  name: data.prospect.name,
  logoText: data.prospect.logoText?.trim() || data.prospect.name,
  logoDataUri: data.prospect.logoDataUri?.trim() || "",
};

export const COVER = { tagline: data.cover.tagline };

/* ─────────────────────────────  THE RESEARCH (PROSPECT)  ───────────────────────────── */

export const OFFERING = {
  headline: data.research.offering.headline,
  bullets: data.research.offering.bullets,
  core: data.research.offering.core,
};

export const ICP = data.research.icp;

export const BUYING_GROUP = data.research.buyingGroup;

/* ─────────────────────────────  THE WORKED ACCOUNT (TARGET)  ───────────────────────────── */

export const ACCOUNT = {
  name: data.target.name,
  ticker: data.target.ticker || "",
  blurb: data.target.blurb,
  stats: data.target.stats,
  gate: data.target.gate,
};

export const SIGNALS = data.target.signals;

export type ResolvedContact = RawContact;
export const CONTACTS: ResolvedContact[] = data.target.contacts;

/* ─────────────────────────────  OUTREACH  ───────────────────────────── */

export type EmailDraft = { to: string; address: string; subject: string; body: React.ReactNode };
export type LinkedInDraft = { to: string; title: string; initials: string; body: React.ReactNode };

export const EMAIL_DRAFTS: EmailDraft[] = data.outreach.email.map((d) => ({
  to: d.to,
  address: d.address,
  subject: d.subject,
  body: renderHighlights(d.body, "pill"),
}));

export const LINKEDIN_DRAFTS: LinkedInDraft[] = data.outreach.linkedin.map((d) => ({
  to: d.to,
  title: d.title,
  initials: d.initials,
  body: renderHighlights(d.body, "pill"),
}));
