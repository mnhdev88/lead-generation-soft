// Tests for the plain helpers in api.ts that decide what the company panel and the tables say
// about website facts and intent scores. No browser needed. Run from frontend/:
//     node --test src/lib/api.test.mjs
// (Node 22.18+ reads the TypeScript file directly.)
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  intentBadgeSpeech,
  mayUseTagManager,
  notFoundText,
  oldCrawlNote,
  salesFactRows,
  siteFactsUsable,
  websiteNotRead,
} from "./api.ts";

const rows = (signals, kind) => Object.fromEntries(salesFactRows(signals, kind));
const NEVER_SAID = /^(No |Missing)/; // a flat "No online booking" / "Missing" is never shown

// A crawl of 2 pages that found nothing (static HTML only, no Tag Manager).
const EMPTY_CRAWL = {
  pages_crawled: 2,
  has_viewport: true,
  has_online_booking: false,
  booking_provider: null,
  has_online_payment: false,
  has_shop: false,
  has_online_ordering: false,
  has_pdf_forms: false,
  has_live_chat: false,
  ad_pixels: [],
  has_analytics: false,
  has_tag_manager: false,
  has_search_console: false,
  has_local_schema: false,
};

test("found facts are stated plainly", () => {
  const r = rows({ ...EMPTY_CRAWL, has_online_booking: true, booking_provider: "zocdoc", has_live_chat: true });
  assert.equal(r["Online booking"], "Yes (zocdoc)");
  assert.equal(r["Live chat"], "Yes");
  assert.equal(r["Mobile-friendly"], "Yes");
});

test("booking not found is never a flat 'No online booking'", () => {
  // A dental office whose booking button is added by a script: we only say we did not see it.
  const r = rows(EMPTY_CRAWL, "appointments");
  assert.equal(r["Online booking"], "Not found on the 2 pages we read (it may be added by a script)");
  for (const text of Object.values(r)) assert.doesNotMatch(text, NEVER_SAID);
});

test("booking / ordering / shop / payment rows only for the kinds intent.py scores", () => {
  const unknown = rows(EMPTY_CRAWL, null);
  assert.equal(unknown["Online booking"], undefined);
  assert.equal(unknown["Online payments"], undefined);
  assert.equal(unknown["Online ordering"], undefined);
  assert.equal(unknown["Online shop"], undefined);

  const restaurant = rows(EMPTY_CRAWL, "food");
  assert.equal(restaurant["Online booking"], undefined);
  assert.match(restaurant["Online ordering"], /^Not found on the 2 pages/);
  assert.match(restaurant["Online payments"], /^Not found/);
  assert.equal(restaurant["Online shop"], undefined);

  const store = rows(EMPTY_CRAWL, "retail");
  assert.equal(store["Online booking"], undefined);
  assert.match(store["Online shop"], /^Not found/);
});

test("Tag Manager sites: analytics label, no ads row, no chat row", () => {
  const gtm = { ...EMPTY_CRAWL, has_analytics: true, has_tag_manager: true };
  const r = rows(gtm, "appointments");
  assert.equal(r["Visitor tracking"], "Yes — Google Analytics / Tag Manager");
  assert.equal(r["Advertising"], undefined); // pixels may be inside the container
  assert.equal(r["Live chat"], undefined); // chat may be loaded by the container
  // Older crawls have no has_tag_manager flag: analytics might be Tag Manager, so stay careful.
  const old = { ...gtm };
  delete old.has_tag_manager;
  assert.equal(mayUseTagManager(old), true);
  assert.equal(rows(old)["Advertising"], undefined);
});

test("no Tag Manager: ads and chat not found are said carefully", () => {
  const r = rows({ ...EMPTY_CRAWL, has_analytics: true }, null);
  assert.equal(r["Visitor tracking"], "Yes — Google Analytics");
  assert.equal(r["Advertising"], "None seen in the page code");
  assert.match(r["Live chat"], /^Not found on the 2 pages we read/);
  assert.equal(rows({ ...EMPTY_CRAWL, ad_pixels: ["meta"] })["Advertising"], "Runs Facebook/Instagram ads");
});

test("schema and viewport not found are not a flat 'Missing' / 'No'", () => {
  const r = rows({ ...EMPTY_CRAWL, has_viewport: false });
  assert.match(r["Business info for Google"], /^Not seen in the page code/);
  assert.equal(r["Mobile-friendly"], "Not found on the home page (no mobile viewport tag)");
  for (const text of Object.values(r)) assert.doesNotMatch(text, NEVER_SAID);
});

test("unknown facts (older crawls) are left out", () => {
  assert.deepEqual(salesFactRows({ pages_crawled: 3 }, "appointments"), []);
});

test("notFoundText counts pages", () => {
  assert.equal(notFoundText(1), "Not found on the 1 page we read (it may be added by a script)");
  assert.equal(notFoundText(6), "Not found on the 6 pages we read (it may be added by a script)");
});

test("website facts need a website and a good crawl, like intent.py", () => {
  const signals = { pages_crawled: 2 };
  assert.equal(siteFactsUsable({ website: "https://a.com", website_signals: signals }), true);
  assert.equal(siteFactsUsable({ website: null, website_signals: signals }), false); // now "No website"
  assert.equal(siteFactsUsable({ website: "https://a.com", website_signals: {} }), false);
});

test("facts kept from an older crawl say so", () => {
  assert.equal(oldCrawlNote({ enrich_status: "done" }), null);
  assert.match(oldCrawlNote({ enrich_status: "failed" }), /earlier successful crawl.*re-crawl failed/);
  assert.equal(oldCrawlNote({ enrich_status: null }), "From an earlier crawl.");
});

test("website not read yet: intent scores are not final", () => {
  assert.equal(websiteNotRead({ website: "https://salon.com", website_signals: {} }), true);
  assert.equal(websiteNotRead({ website: "https://salon.com", website_signals: { pages_crawled: 0 } }), true);
  assert.equal(websiteNotRead({ website: "https://salon.com", website_signals: { pages_crawled: 2 } }), false);
  assert.equal(websiteNotRead({ website: null, website_signals: {} }), false); // nothing to read
});

test("intent badge speech has the score, hot, the reasons and the not-read hint", () => {
  assert.equal(
    intentBadgeSpeech({ score: 72, label: "Website", reasons: ["No SSL +15", "Slow on mobile +15"] }),
    "Website intent 72 out of 100, hot lead. No SSL +15. Slow on mobile +15",
  );
  assert.equal(intentBadgeSpeech({ score: 10 }), "Intent 10 out of 100");
  assert.match(intentBadgeSpeech({ score: 0, unchecked: true }), /Website not read yet/);
});
