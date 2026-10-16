import { NextResponse, type NextRequest } from "next/server";

// Without a login cookie, every page sends you to /login first. This is only a quick
// first check: the backend checks the session itself on every /api call (an old or fake
// cookie gets past this, but then every data request is refused and sends you to /login).
const COOKIES = ["__Host-leadgen_session", "leadgen_session"];

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const hasSession = COOKIES.some((name) => request.cookies.has(name));
  if (!hasSession && pathname !== "/login") {
    const url = new URL("/login", request.url);
    if (pathname !== "/") url.searchParams.set("next", pathname + search);
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  // Everything except the API (protected by the backend) and static files.
  matcher: ["/((?!api/|_next/static|_next/image|favicon.ico).*)"],
};
