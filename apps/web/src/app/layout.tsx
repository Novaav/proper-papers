import type { Metadata } from "next";
import { cookies } from "next/headers.js";
import Link from "next/link.js";
import type { ReactNode } from "react";

import "./styles/index.css";
import { readSession } from "@/lib/auth/server.ts";
import { parseTheme, THEME_COOKIE } from "@/lib/theme.ts";
import { BrandMark } from "./brand-mark.tsx";
import { ThemeToggle } from "./theme-toggle.tsx";

// Every page is rendered per request, so Next.js can apply the per-request
// CSP nonce from src/proxy.ts to its scripts (a prerendered page, such as the
// default 404, would carry no nonce and its scripts would be blocked).
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "Proper Papers",
  description:
    "Proper Papers: evidence-grounded document intelligence. Every claim has a source: structured records in which every value links to its source text.",
};

// The header only needs to know whether a session exists, to say "Sign in" or
// "Account"; it never calls the API. Every page renders
// <main id="main" tabIndex={-1}>, the skip link's target.
export default async function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  const signedIn = (await readSession()) !== null;
  // The visitor's theme choice, if any, goes on <html> in the server's HTML so
  // the first paint is right; without one the system preference decides.
  const theme = parseTheme((await cookies()).get(THEME_COOKIE)?.value);
  return (
    <html lang="en" data-theme={theme ?? undefined}>
      <body>
        <a className="skip-link" href="#main">
          Skip to main content
        </a>
        <header className="app-header">
          <div className="app-header-inner">
            <Link className="brand" href="/">
              <BrandMark />
              <span>Proper Papers</span>
            </Link>
            <nav aria-label="Main" className="app-nav">
              <ul>
                <li>
                  <ThemeToggle variant="header" />
                </li>
                <li>
                  <a href="/account">{signedIn ? "Account" : "Sign in"}</a>
                </li>
              </ul>
            </nav>
          </div>
        </header>
        {children}
      </body>
    </html>
  );
}
