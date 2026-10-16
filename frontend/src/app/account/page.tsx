import type { Metadata } from "next";
import { AccountPage } from "@/components/auth/AccountPage";

export const metadata: Metadata = { title: "Your account" };

export default function Page() {
  return <AccountPage />;
}
