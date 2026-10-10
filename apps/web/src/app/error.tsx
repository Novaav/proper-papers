"use client";

import Link from "next/link.js";

// Error boundary for pages under the root layout (/, /account). Replaces
// Next.js's built-in error UI (which uses inline style props) with one that
// needs no inline styles under the Content-Security-Policy. Shows no error
// details; Next.js logs them on the server.
export default function RootError({ reset }: { reset: () => void }) {
  return (
    <main id="main" tabIndex={-1} className="card message">
      <h1>Something went wrong</h1>
      <p role="alert">This page could not be shown.</p>
      <p>
        <button type="button" onClick={reset}>
          Try again
        </button>{" "}
        <Link href="/">Go to the home page</Link>
      </p>
    </main>
  );
}
