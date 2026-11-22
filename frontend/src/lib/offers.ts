// Type-only import, so `node --test src/lib/offers.test.mjs` can load this file on its own.
import type { Offer } from "@/lib/api";

// The same order as OFFERS in api.ts.
const ORDER: readonly Offer[] = ["website", "seo", "software", "marketing"];

/** "What are you selling?" in the search form: per service, what the app checks on each
 * business (see backend/app/intent.py) and business types that often need it. */
export const OFFER_CHECKS: Record<Offer, string> = {
  website: "no website, no SSL, outdated or slow site, not mobile-friendly, DIY builder",
  seo: "missing title/description, no analytics, weak Google profile, low Google position",
  software: "no online booking/ordering/payment, PDF forms, hiring office staff, review complaints",
  marketing: "runs ads, hiring marketing, low rating, fewer reviews than competitors",
};

// Business types that often buy each service, most likely first.
export const OFFER_SUGGESTIONS: Record<Offer, string[]> = {
  website: ["plumber", "electrician", "roofer", "landscaper", "cleaning service", "auto repair", "bakery", "tailor", "contractor", "salon"],
  seo: ["dentist", "lawyer", "chiropractor", "real estate agent", "plumber", "roofer", "hvac", "med spa", "physiotherapist", "accountant"],
  software: ["clinic", "dentist", "salon", "spa", "gym", "restaurant", "veterinarian", "physiotherapist", "school", "hotel"],
  marketing: ["restaurant", "gym", "hotel", "real estate agent", "med spa", "car dealer", "boutique", "cafe", "wedding venue", "furniture store"],
};

const MAX_SUGGESTIONS = 12;

/** Business types to suggest for the services picked: types that fit more of them first,
 * then the most likely ones; leaving out types already added. */
export function suggestTypes(offers: readonly Offer[], already: readonly string[]): string[] {
  const picked = ORDER.filter((o) => offers.includes(o));
  const rank = new Map<string, { fits: number; best: number }>();
  for (const offer of picked) {
    OFFER_SUGGESTIONS[offer].forEach((type, i) => {
      const r = rank.get(type) ?? { fits: 0, best: i };
      rank.set(type, { fits: r.fits + 1, best: Math.min(r.best, i) });
    });
  }
  return [...rank.entries()]
    .filter(([type]) => !already.includes(type))
    .sort(([, a], [, b]) => b.fits - a.fits || a.best - b.best)
    .map(([type]) => type)
    .slice(0, MAX_SUGGESTIONS);
}

/** "Website + SEO" */
export function offersLabel(offers: readonly Offer[], labels: Record<Offer, string>): string {
  return ORDER.filter((o) => offers.includes(o))
    .map((o) => labels[o])
    .join(" + ");
}
