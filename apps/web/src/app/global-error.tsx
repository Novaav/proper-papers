"use client";

import "./styles/index.css";

// Shown only when the root layout itself fails; it replaces the whole
// document, so it renders its own <html> and <body> and imports the global
// stylesheet. Replaces Next.js's built-in version (which uses inline style
// props) with one that needs no inline styles. Shows no error details.
export default function GlobalError({ reset }: { reset: () => void }) {
  return (
    <html lang="en">
      <body>
        <title>Something went wrong – Proper Papers</title>
        <main id="main" tabIndex={-1} className="card message">
          <h1>Something went wrong</h1>
          <p role="alert">The application could not be shown.</p>
          <p>
            <button type="button" onClick={reset}>
              Try again
            </button>
          </p>
        </main>
      </body>
    </html>
  );
}
