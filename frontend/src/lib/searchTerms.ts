// Turns pasted lists of business types and places into clean one-per-line values.
// Plain functions, tested in searchTerms.test.mjs.

const US_STATES = [
  "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
  "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
  "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
  "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma",
  "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
  "virginia", "washington", "west virginia", "wisconsin", "wyoming",
];
const STATE_CODES = [
  "al", "ak", "az", "ar", "ca", "co", "ct", "dc", "de", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky", "la",
  "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or",
  "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
  // Canada
  "ab", "bc", "mb", "nb", "nl", "ns", "on", "pe", "qc", "sk",
];
const OTHER_STATES = [
  // Canada
  "alberta", "british columbia", "manitoba", "new brunswick", "newfoundland", "nova scotia", "ontario", "quebec",
  "saskatchewan",
  // Pakistan
  "sindh", "punjab", "balochistan", "khyber pakhtunkhwa", "kpk",
  // India (Delhi is left out on purpose: in a list it is a city)
  "andhra pradesh", "assam", "bihar", "goa", "gujarat", "haryana", "karnataka", "kerala", "madhya pradesh",
  "maharashtra", "odisha", "rajasthan", "tamil nadu", "telangana", "uttar pradesh", "west bengal",
];
const COUNTRIES = [
  "usa", "us", "united states", "united states of america", "america", "canada", "pakistan", "india", "uk",
  "united kingdom", "england", "scotland", "wales", "ireland", "australia", "new zealand", "uae",
  "united arab emirates", "saudi arabia", "qatar", "bangladesh", "germany", "france", "spain", "italy", "netherlands",
];
// Written in capitals: "austin, tx" -> "Austin, TX".
const CAPITALS = new Set([...STATE_CODES, "usa", "us", "uk", "uae", "kpk"]);
const STATES = new Set([...US_STATES, ...STATE_CODES, ...OTHER_STATES]);
const COUNTRY_SET = new Set(COUNTRIES);
// Names that are also famous cities: "Chicago, New York, Boston" is three cities.
const ALSO_A_CITY = new Set(["new york", "washington", "quebec", "la"]);

const CITY = 0;
const STATE = 1;
const COUNTRY = 2;

function level(part: string): number {
  const key = part.toLowerCase();
  return COUNTRY_SET.has(key) ? COUNTRY : STATES.has(key) ? STATE : CITY;
}

/** Removes list markers, wrapping quotes and stray end punctuation: `2) "Lahore".` -> `Lahore`. */
function clean(text: string): string {
  return text
    .replace(/^\s*(?:[-*•·–—]+|\d+[.)])\s+/, "")
    .replace(/[.:]+\s*$/, "")
    .replace(/^["'“‘]+|["'”’]+$/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function capitalise(text: string): string {
  return text.toLowerCase().replace(/(^|[\s-])(\p{L})/gu, (_, gap: string, letter: string) => gap + letter.toUpperCase());
}

/** "austin" -> "Austin", "KARACHI" -> "Karachi", "tx" -> "TX". Mixed capitals ("McAllen") stay as typed. */
function tidyCase(part: string): string {
  if (CAPITALS.has(part.toLowerCase())) return part.toUpperCase();
  const allOneCase = part === part.toLowerCase() || part === part.toUpperCase();
  return allOneCase ? capitalise(part) : part;
}

function tidyPlace(place: string): string {
  return place
    .split(",")
    .map((part) => tidyCase(clean(part)))
    .filter(Boolean)
    .join(", ");
}

function unique(items: string[]): string[] {
  const seen = new Map<string, string>();
  for (const item of items) if (item && !seen.has(item.toLowerCase())) seen.set(item.toLowerCase(), item);
  return [...seen.values()];
}

/** Puts the parts of a list together: a state or country joins the place before it. */
function groupParts(parts: string[], alsoCityJoins: boolean): string[][] {
  const places: string[][] = [];
  for (const part of parts) {
    const place = places[places.length - 1];
    const lvl = level(part);
    const last = place?.[place.length - 1] ?? "";
    // The first word of a place counts as its city: "Washington, DC", "New York, New York".
    const lastLevel = place?.length === 1 && ALSO_A_CITY.has(last.toLowerCase()) ? CITY : level(last);
    const mayJoin = alsoCityJoins || !ALSO_A_CITY.has(part.toLowerCase());
    if (place && lvl > lastLevel && mayJoin) place.push(part);
    else places.push([part]);
  }
  return places;
}

/** One line (or spreadsheet row) of a pasted list -> its places. */
function splitLine(line: string): string[] {
  const parts = line
    .split(/[,\t]+|\s+(?:and|&)\s+/i)
    .map(clean)
    .filter(Boolean);
  let places = groupParts(parts, true);
  // A city name read as a state, next to places with no state at all: they were all cities.
  const joinedCityName = places.some((p) => p.slice(1).some((x) => ALSO_A_CITY.has(x.toLowerCase())));
  if (joinedCityName && places.some((p) => p.length === 1)) places = groupParts(parts, false);
  // "Karachi, Lahore, Pakistan": a state or country only at the very end belongs to all of them.
  const last = places[places.length - 1];
  if (places.length > 1 && last.length > 1 && places.slice(0, -1).every((p) => p.length === 1)) {
    places = places.map((p) => [p[0], ...last.slice(1)]);
  }
  return places.map((p) => p.join(", "));
}

/** A pasted list of places -> one tidy place each. Several lines are taken as one place per line
 * (keeping "Clifton, Karachi" whole). A single line is split at its commas, keeping
 * "Austin, Texas" whole. Spreadsheet rows, bullets, numbering and repeats are all handled. */
export function parsePlaces(text: string): string[] {
  const lines = text
    .split(/[\r\n;|]+/)
    .map((line) => line.trim())
    .filter(Boolean);
  const places = lines.flatMap((line) => (lines.length === 1 || line.includes("\t") ? splitLine(line) : [line]));
  return unique(places.map(tidyPlace));
}

/** What's in the Places box -> the places searched: one per line, exactly as the lines say. */
export function placeLines(text: string): string[] {
  return unique(text.split(/\r?\n/).map(tidyPlace));
}

/** A pasted list of business types -> tidy lowercase types: "Dentists, GYM\n• salon". */
export function parseBusinessTypes(text: string): string[] {
  const types = text
    .split(/[\r\n,;|\t]+/)
    .map((t) => clean(t).toLowerCase())
    .filter((t) => t.length >= 2);
  return unique(types);
}
