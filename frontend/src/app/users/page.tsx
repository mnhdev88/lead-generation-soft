import type { Metadata } from "next";
import { UsersPage } from "@/components/auth/UsersPage";

export const metadata: Metadata = { title: "Users" };

export default function Page() {
  return <UsersPage />;
}
