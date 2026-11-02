// Tests for how pasted business types and places are read. No browser needed. Run from frontend/:
//     node --test src/lib/searchTerms.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { parseBusinessTypes, parsePlaces, placeLines } from "./searchTerms.ts";

test("a comma list of cities becomes one place each", () => {
  assert.deepEqual(parsePlaces("Karachi, Lahore, Islamabad"), ["Karachi", "Lahore", "Islamabad"]);
  assert.deepEqual(parsePlaces("Karachi,Lahore"), ["Karachi", "Lahore"]);
  assert.deepEqual(parsePlaces("Karachi, Lahore and Multan"), ["Karachi", "Lahore", "Multan"]);
});

test("a city with its state or country stays together", () => {
  assert.deepEqual(parsePlaces("Austin, Texas"), ["Austin, Texas"]);
  assert.deepEqual(parsePlaces("Karachi, Sindh, Pakistan"), ["Karachi, Sindh, Pakistan"]);
  assert.deepEqual(parsePlaces("Austin, TX, Dallas, TX"), ["Austin, TX", "Dallas, TX"]);
  assert.deepEqual(parsePlaces("Karachi, Pakistan, Austin, Texas, USA"), ["Karachi, Pakistan", "Austin, Texas, USA"]);
});

test("a state or country at the very end applies to every city", () => {
  assert.deepEqual(parsePlaces("Karachi, Lahore, Pakistan"), ["Karachi, Pakistan", "Lahore, Pakistan"]);
  assert.deepEqual(parsePlaces("Austin, Dallas, TX"), ["Austin, TX", "Dallas, TX"]);
});

test("a list of states or countries is not joined together", () => {
  assert.deepEqual(parsePlaces("Texas, Florida, Ohio"), ["Texas", "Florida", "Ohio"]);
  assert.deepEqual(parsePlaces("Pakistan, India"), ["Pakistan", "India"]);
  assert.deepEqual(parsePlaces("Texas, USA"), ["Texas, USA"]);
});

test("names that are both a city and a state", () => {
  assert.deepEqual(parsePlaces("Chicago, New York, Boston"), ["Chicago", "New York", "Boston"]);
  assert.deepEqual(parsePlaces("Boston, Chicago, New York"), ["Boston", "Chicago", "New York"]);
  assert.deepEqual(parsePlaces("Albany, New York, Buffalo, New York"), ["Albany, New York", "Buffalo, New York"]);
  assert.deepEqual(parsePlaces("Washington, DC"), ["Washington, DC"]);
  assert.deepEqual(parsePlaces("New York, New York"), ["New York, New York"]);
});

test("several lines mean one place per line, even with commas", () => {
  assert.deepEqual(parsePlaces("Austin, Texas\nDallas, Texas\r\n\r\nClifton, Karachi\n"), [
    "Austin, Texas",
    "Dallas, Texas",
    "Clifton, Karachi",
  ]);
  assert.deepEqual(parsePlaces("Karachi; Lahore | Multan"), ["Karachi", "Lahore", "Multan"]);
});

test("spreadsheet columns, bullets and numbering are understood", () => {
  assert.deepEqual(parsePlaces("Austin\tTX\nDallas\tTX"), ["Austin, TX", "Dallas, TX"]);
  assert.deepEqual(parsePlaces("Karachi\tLahore\tMultan"), ["Karachi", "Lahore", "Multan"]);
  assert.deepEqual(parsePlaces("1. Karachi\n2) Lahore\n- Multan\n• Quetta"), ["Karachi", "Lahore", "Multan", "Quetta"]);
  assert.deepEqual(parsePlaces('"Karachi"\n"Lahore".'), ["Karachi", "Lahore"]);
});

test("capitals are tidied and repeats dropped", () => {
  assert.deepEqual(parsePlaces("austin, tx\nKARACHI\nkarachi\nMcAllen, texas"), ["Austin, TX", "Karachi", "McAllen, Texas"]);
  assert.deepEqual(parsePlaces("rahim yar khan, pakistan, usa"), ["Rahim Yar Khan, Pakistan", "USA"]);
});

test("empty input gives nothing", () => {
  assert.deepEqual(parsePlaces("  \n , \n"), []);
  assert.deepEqual(placeLines(" \n"), []);
  assert.deepEqual(parseBusinessTypes(" , ;"), []);
});

test("placeLines keeps what you typed on a line together", () => {
  assert.deepEqual(placeLines("Clifton, Karachi\n\n  lahore \nLAHORE"), ["Clifton, Karachi", "Lahore"]);
});

test("business types are split, lowercased and de-duplicated", () => {
  assert.deepEqual(parseBusinessTypes("Dentist, GYM\n• Car Repair;salon\tdentist"), ["dentist", "gym", "car repair", "salon"]);
  assert.deepEqual(parseBusinessTypes("x, gym"), ["gym"]); // the backend needs at least 2 letters
});
