import type { NextConfig } from "next";

// Authentication responses and pages that show account data are never cached.
const noStore = { key: "Cache-Control", value: "no-store" };

const nextConfig: NextConfig = {
  poweredByHeader: false,
  async headers() {
    return [
      {
        // Baseline for every response. The Content-Security-Policy (with a
        // per-request nonce) is set in src/proxy.ts. HSTS is left to the
        // production HTTPS front end (docs/deployment-security.md).
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
          { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
        ],
      },
      {
        source: "/auth/:path*",
        // Callback URLs carry the authorization code; never pass them on as a referrer.
        headers: [noStore, { key: "Referrer-Policy", value: "no-referrer" }],
      },
      {
        source: "/account",
        // Not "no-referrer": under that policy browsers send `Origin: null` even on
        // same-origin form POSTs, and the logout Origin check would (correctly) refuse
        // sign-out. "same-origin" keeps the real Origin for our own POSTs and sends no
        // referrer to other sites.
        headers: [noStore, { key: "Referrer-Policy", value: "same-origin" }],
      },
      {
        // Tenant pages post forms too, so the same reasoning as /account applies.
        source: "/tenants/:path*",
        headers: [noStore, { key: "Referrer-Policy", value: "same-origin" }],
      },
    ];
  },
  logging: {
    // The development server logs request URLs; the callback URL carries the
    // one-time authorization code and state, which must not reach logs.
    incomingRequests: { ignore: [/^\/auth\/callback/] },
  },
};

export default nextConfig;
