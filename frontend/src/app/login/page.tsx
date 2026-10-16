import type { Metadata } from "next";
import { Suspense } from "react";
import { LoginForm } from "@/components/auth/LoginForm";

export const metadata: Metadata = {
  title: "Log in",
  robots: { index: false, follow: false },
};

export default function Page() {
  // LoginForm reads ?next= from the URL (useSearchParams), which needs a Suspense boundary.
  return (
    <main className="flex flex-1 items-center justify-center px-4 py-12">
      <Suspense fallback={null}>
        <LoginForm />
      </Suspense>
    </main>
  );
}
