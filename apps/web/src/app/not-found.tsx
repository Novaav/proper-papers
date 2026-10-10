import Link from "next/link.js";

// Replaces Next.js's built-in 404 page, which uses inline style attributes
// and an un-nonced <style> tag that the Content-Security-Policy (correctly)
// blocks. Styled only by the application stylesheet (styles/).
export default function NotFound() {
  return (
    <main id="main" tabIndex={-1} className="card message">
      <h1>Page not found</h1>
      <p>This page does not exist.</p>
      <p>
        <Link href="/">Go to the home page</Link>
      </p>
    </main>
  );
}
