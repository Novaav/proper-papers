// Sets the per-request Content-Security-Policy (Next.js's documented nonce
// approach). It does no authentication and makes no decisions about users;
// pages and route handlers do that themselves.
import { NextResponse, type NextRequest } from "next/server.js";

import {
  buildContentSecurityPolicy,
  generateNonce,
} from "@/lib/security/csp.ts";

export function proxy(request: NextRequest) {
  const csp = buildContentSecurityPolicy({
    nonce: generateNonce(),
    development: process.env.NODE_ENV === "development",
    issuer: process.env.OIDC_ISSUER,
  });
  // Next.js reads the nonce from the request's CSP header while rendering.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      // Everything except build assets, which need no CSP of their own, and
      // the three file routes, which render no page and set their own
      // headers:
      // - upload: with a proxy, Next.js buffers request bodies up to 10 MB
      //   and truncates the rest, which would corrupt uploads of up to
      //   20 MiB. It answers only with redirects or plain text.
      // - download and the approved-record export: the proxy's page policy
      //   would replace the route's "sandbox; default-src 'none'" on the
      //   served file.
      source:
        "/((?!_next/static|_next/image|favicon.ico|tenants/[^/]+/workspaces/[^/]+/documents/(?:upload|[^/]+/download|[^/]+/review/approvals/[^/]+/export)$).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
