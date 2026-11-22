// Tests for the "What are you selling?" suggestions. No browser needed. Run from frontend/:
//     node --test src/lib/offers.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { OFFER_SUGGESTIONS, offersLabel, suggestTypes } from "./offers.ts";

const LABELS = { website: "Website", seo: "SEO", software: "Software", marketing: "Marketing" };

test("nothing picked, nothing suggested", () => {
  assert.deepEqual(suggestTypes([], []), []);
});

test("one service: its list, most likely first", () => {
  assert.deepEqual(suggestTypes(["seo"], []), OFFER_SUGGESTIONS.seo);
});

test("types that fit several services come first, and added types are left out", () => {
  const got = suggestTypes(["software", "seo"], ["clinic"]);
  assert.equal(got[0], "dentist"); // on both lists
  assert.ok(got.includes("physiotherapist")); // on both lists too
  assert.ok(got.indexOf("physiotherapist") < got.indexOf("lawyer"));
  assert.ok(!got.includes("clinic"));
  assert.ok(got.length <= 12);
  assert.equal(new Set(got).size, got.length);
});

test("labels in the usual order", () => {
  assert.equal(offersLabel(["seo", "website"], LABELS), "Website + SEO");
});
