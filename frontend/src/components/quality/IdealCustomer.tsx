"use client";

import { useEffect, useState } from "react";
import { api, SIZE_BAND_LABEL, SIZE_BANDS, type IdealCustomer, type SizeBand } from "@/lib/api";

/** "dentist, orthodontist" <-> ["dentist", "orthodontist"] */
const toWords = (text: string) =>
  text
    .split(/[,\n]/)
    .map((w) => w.trim())
    .filter(Boolean);

type Form = { types: string; exclude: string; places: string; sizes: SizeBand[]; minRating: string; minReviews: string };

const fromProfile = (p: IdealCustomer): Form => ({
  types: p.business_types.join(", "),
  exclude: p.exclude_types.join(", "),
  places: p.places.join(", "),
  sizes: p.sizes,
  minRating: p.min_rating === null ? "" : String(p.min_rating),
  minReviews: p.min_reviews === null ? "" : String(p.min_reviews),
});

/** What a good lead looks like for you: leads outside it are marked "poor fit" and score lower. */
export function IdealCustomerSection() {
  const [form, setForm] = useState<Form | null>(null);
  const [counts, setCounts] = useState<{ good: number; poor: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getIdealCustomer().then(
      (r) => {
        setForm(fromProfile(r.profile));
        setCounts({ good: r.good, poor: r.poor });
      },
      (err) => setError(err instanceof Error ? err.message : "Could not load your ideal customer."),
    );
  }, []);

  if (!form) {
    return (
      <section className="card space-y-2">
        <h2 className="text-base font-semibold">Your ideal customer</h2>
        <p className="text-sm text-[var(--muted)]">{error ?? "Loading…"}</p>
      </section>
    );
  }

  const set = (changes: Partial<Form>) => {
    setForm({ ...form, ...changes });
    setSaved(false);
  };

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!form) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.saveIdealCustomer({
        business_types: toWords(form.types),
        exclude_types: toWords(form.exclude),
        places: toWords(form.places),
        sizes: form.sizes,
        min_rating: form.minRating === "" ? null : Number(form.minRating),
        min_reviews: form.minReviews === "" ? null : Number(form.minReviews),
      });
      setForm(fromProfile(r.profile));
      setCounts({ good: r.good, poor: r.poor });
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card space-y-3">
      <div>
        <h2 className="text-base font-semibold">Your ideal customer</h2>
        <p className="text-sm text-[var(--muted)]">
          Describe the businesses you want to sell to. Leads outside it are marked “Poor fit”, lose 20 points, and can be
          hidden with the Fit filter on the Search page. Leave a box empty for “anything”. Something we don&apos;t know
          yet (no size or rating) never counts against a lead.
        </p>
      </div>
      <form onSubmit={save} className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1">
          <span className="label">Business types you target</span>
          <input
            className="input"
            value={form.types}
            onChange={(e) => set({ types: e.target.value })}
            placeholder="dentist, orthodontist, clinic"
          />
        </label>
        <label className="space-y-1">
          <span className="label">Business types to leave out</span>
          <input
            className="input"
            value={form.exclude}
            onChange={(e) => set({ exclude: e.target.value })}
            placeholder="hospital, school"
          />
        </label>
        <label className="space-y-1 sm:col-span-2">
          <span className="label">Places (city, state or country words)</span>
          <input
            className="input"
            value={form.places}
            onChange={(e) => set({ places: e.target.value })}
            placeholder="Austin, Dallas, Texas"
          />
        </label>
        <fieldset className="space-y-1 sm:col-span-2">
          <legend className="label">Sizes</legend>
          <div className="flex flex-wrap gap-3">
            {SIZE_BANDS.map((b) => (
              <label key={b} className="flex items-center gap-1.5 text-sm">
                <input
                  type="checkbox"
                  className="checkbox"
                  checked={form.sizes.includes(b)}
                  onChange={(e) =>
                    set({ sizes: e.target.checked ? [...form.sizes, b] : form.sizes.filter((s) => s !== b) })
                  }
                />
                {SIZE_BAND_LABEL[b]}
              </label>
            ))}
          </div>
        </fieldset>
        <label className="space-y-1">
          <span className="label">Lowest Google rating</span>
          <input
            className="input"
            type="number"
            min={0}
            max={5}
            step={0.1}
            value={form.minRating}
            onChange={(e) => set({ minRating: e.target.value })}
            placeholder="e.g. 4.0"
          />
        </label>
        <label className="space-y-1">
          <span className="label">Fewest Google reviews</span>
          <input
            className="input"
            type="number"
            min={0}
            step={1}
            value={form.minReviews}
            onChange={(e) => set({ minReviews: e.target.value })}
            placeholder="e.g. 10"
          />
        </label>
        <div className="flex flex-wrap items-center gap-3 sm:col-span-2">
          <button className="btn-primary" disabled={busy}>
            {busy ? "Saving and re-scoring…" : "Save"}
          </button>
          {counts && (counts.good > 0 || counts.poor > 0) && (
            <span role="status" className={`text-sm ${saved ? "text-[var(--success)]" : "text-[var(--muted)]"}`}>
              {saved && "Saved. "}
              {counts.good.toLocaleString()} ideal {counts.good === 1 ? "customer" : "customers"},{" "}
              {counts.poor.toLocaleString()} poor {counts.poor === 1 ? "fit" : "fits"} among your saved businesses.
            </span>
          )}
          {saved && counts && counts.good === 0 && counts.poor === 0 && (
            <span role="status" className="text-sm text-[var(--success)]">
              Saved. No profile set: every lead counts.
            </span>
          )}
        </div>
      </form>
      {error && (
        <p role="alert" className="text-sm text-[var(--danger)]">
          {error}
        </p>
      )}
    </section>
  );
}
