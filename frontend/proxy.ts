import { NextResponse, type NextRequest } from "next/server";

/**
 * Route protection for signed-in pages. This only checks that a session cookie exists so
 * signed-out visitors are redirected before the page renders; every API call is still
 * authenticated and authorised by the backend, which is the real security boundary.
 */
const SESSION_COOKIE = "sta_session";

export function proxy(request: NextRequest) {
  if (request.cookies.has(SESSION_COOKIE)) return NextResponse.next();
  const url = request.nextUrl.clone();
  url.pathname = "/login";
  url.search = `?next=${encodeURIComponent(request.nextUrl.pathname + request.nextUrl.search)}`;
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/dashboard/:path*", "/searches/:path*", "/opportunities/:path*", "/notifications/:path*", "/settings/:path*"],
};
