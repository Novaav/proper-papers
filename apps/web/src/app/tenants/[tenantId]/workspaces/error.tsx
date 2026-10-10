"use client";

// Unexpected errors only (expected API failures are rendered by the page).
// Shows no error details; Next.js logs them on the server.
export default function WorkspacesError({ reset }: { reset: () => void }) {
  return (
    <main id="main" tabIndex={-1} className="card message">
      <h1>Something went wrong</h1>
      <p role="alert">The workspaces could not be shown.</p>
      <p>
        <button type="button" onClick={reset}>
          Try again
        </button>{" "}
        <a href="/account">Back to your account</a>
      </p>
    </main>
  );
}
