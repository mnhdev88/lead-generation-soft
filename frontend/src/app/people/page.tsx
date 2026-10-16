import type { Metadata } from "next";
import { Suspense } from "react";
import { PeoplePage } from "@/components/people/PeoplePage";

export const metadata: Metadata = {
  title: "People",
  description: "Find decision-makers at local businesses and verify their emails.",
};

export default function Page() {
  // PeoplePage reads ?job_id= from the URL (useSearchParams), which needs a Suspense
  // boundary so the rest of the page can be prerendered.
  return (
    <Suspense fallback={<p className="mx-auto w-full max-w-[1400px] px-4 py-6 text-sm text-[var(--muted)]">Loading…</p>}>
      <PeoplePage />
    </Suspense>
  );
}
