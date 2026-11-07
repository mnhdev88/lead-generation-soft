import type { Metadata } from "next";
import { QualityPage } from "@/components/quality/QualityPage";

export const metadata: Metadata = { title: "Lead quality" };

export default function Page() {
  return <QualityPage />;
}
