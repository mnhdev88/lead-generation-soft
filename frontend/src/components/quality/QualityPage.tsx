"use client";

import { BouncedSection, DoNotContactSection } from "@/components/quality/DoNotContact";
import { IdealCustomerSection } from "@/components/quality/IdealCustomer";

/** Tools that keep the leads clean: your ideal customer, bounced emails, the do-not-contact list. */
export function QualityPage() {
  return (
    <main className="mx-auto w-full max-w-[1000px] space-y-6 px-4 py-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Lead quality</h1>
        <p className="text-sm text-[var(--muted)]">
          Teach LeadGen from your results, so the leads it gives you get better over time.
        </p>
      </header>
      <IdealCustomerSection />
      <BouncedSection />
      <DoNotContactSection />
    </main>
  );
}
