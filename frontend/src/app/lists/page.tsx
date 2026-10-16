import type { Metadata } from "next";
import { Suspense } from "react";
import { ListsPage } from "@/components/lists/ListsPage";

export const metadata: Metadata = {
  title: "Lists",
  description: "Your saved lead lists: businesses and people, with status and export.",
};

export default function Page() {
  // ListsPage reads ?id= from the URL (useSearchParams), which needs a Suspense
  // boundary so the rest of the page can be prerendered.
  return (
    <Suspense fallback={<p className="mx-auto w-full max-w-[1400px] px-4 py-6 text-sm text-[var(--muted)]">Loading…</p>}>
      <ListsPage />
    </Suspense>
  );
}
