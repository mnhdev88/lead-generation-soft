import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Nav } from "@/components/Nav";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  // Pages can set their own title, e.g. "People" becomes "People · LeadGen".
  title: { default: "LeadGen", template: "%s · LeadGen" },
  description:
    "Find local businesses and the people who run them, verify their emails, and organise leads into lists.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      {/* suppressHydrationWarning: browser extensions like Grammarly add attributes to <body> */}
      <body className="min-h-full flex flex-col" suppressHydrationWarning>
        <Nav />
        {children}
      </body>
    </html>
  );
}
