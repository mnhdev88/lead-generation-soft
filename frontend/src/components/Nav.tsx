"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { api, goToLogin, type User } from "@/lib/api";

const LINKS = [
  { href: "/", label: "Search", hint: "Find local businesses" },
  { href: "/people", label: "People", hint: "Find decision-makers and their emails" },
  { href: "/lists", label: "Lists", hint: "Your saved lead lists" },
  { href: "/quality", label: "Quality", hint: "Bounced emails, do-not-contact list, what wins" },
] as const;
const ADMIN_LINKS = [{ href: "/users", label: "Users", hint: "Who can log in" }] as const;

/** Top bar shown on every page (except the login page): app name, sections and your account. */
export function Nav() {
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    if (pathname === "/login") return;
    api.me().then(setUser, (e: Error) => {
      // Cookie present but the session ended: log in again.
      if (e.message === "Please log in.") goToLogin();
    });
  }, [pathname]);

  if (pathname === "/login") return null;

  // "/" only matches the home page itself; "/people" also matches "/people/…".
  const isActive = (href: string) => (href === "/" ? pathname === "/" : pathname.startsWith(href));
  const links = user?.role === "admin" ? [...LINKS, ...ADMIN_LINKS] : LINKS;

  async function logout() {
    await api.logout().catch(() => undefined);
    // A full page load on purpose: nothing from the previous login stays in memory.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign("/login");
  }

  return (
    <header className="sticky top-0 z-30 border-b border-[var(--border)] bg-[var(--card)]/90 backdrop-blur">
      {/* Phones: logo + account on the first row, the sections on a full-width second row. */}
      <div className="mx-auto flex w-full max-w-[1400px] flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2 sm:flex-nowrap sm:gap-6 sm:py-2.5">
        <Link href="/" className="flex shrink-0 items-center gap-2 font-semibold tracking-tight">
          <span
            aria-hidden
            className="grid h-7 w-7 place-items-center rounded-lg bg-[var(--accent)] text-sm font-bold text-[var(--accent-fg)]"
          >
            L
          </span>
          <span>LeadGen</span>
        </Link>
        <nav aria-label="Main" className="order-last -mx-1 w-[calc(100%+0.5rem)] min-w-0 overflow-x-auto sm:order-none sm:mx-0 sm:w-auto">
          <ul className="flex items-center gap-1">
            {links.map((l) => {
              const active = isActive(l.href);
              return (
                <li key={l.href} className="flex-1 sm:flex-none">
                  <Link
                    href={l.href}
                    title={l.hint}
                    aria-current={active ? "page" : undefined}
                    className={`block whitespace-nowrap rounded-lg px-2.5 py-2.5 text-center text-sm font-medium transition-colors sm:px-3 sm:py-1.5 ${
                      active
                        ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                        : "text-[var(--muted)] hover:bg-[var(--muted-bg)] hover:text-[var(--foreground)]"
                    }`}
                  >
                    {l.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
        {user && (
          <div className="ml-auto flex shrink-0 items-center gap-2">
            <Link
              href="/account"
              title={`${user.email}: change your password`}
              aria-current={isActive("/account") ? "page" : undefined}
              className="block max-w-[12ch] truncate rounded-lg px-2 py-1.5 text-sm text-[var(--muted)] hover:bg-[var(--muted-bg)] hover:text-[var(--foreground)] sm:max-w-[16ch]"
            >
              {user.name}
            </Link>
            <button type="button" className="btn-secondary" onClick={logout}>
              Log out
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
