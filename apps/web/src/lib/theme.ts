// The theme choice: "light" or "dark", kept in a plain cookie so the server can
// put it on <html data-theme> before the first paint (no flash, no hydration
// mismatch, no inline script). With no cookie there is no attribute and the
// system preference decides (prefers-color-scheme in tokens.css). The cookie
// holds nothing but the word; it is not a session and grants nothing.
// Client-safe: no authentication, API or document modules.

export type Theme = "light" | "dark";

export const THEME_COOKIE = "theme";
const ONE_YEAR_SECONDS = 60 * 60 * 24 * 365;

/** The theme a cookie value names, or null for anything else (ignored). */
export function parseTheme(value: string | undefined | null): Theme | null {
  return value === "light" || value === "dark" ? value : null;
}

/** The other theme. */
export function otherTheme(current: Theme): Theme {
  return current === "dark" ? "light" : "dark";
}

/** The theme in effect: the visitor's choice, else the system preference. */
export function effectiveTheme(
  chosen: string | undefined | null,
  systemPrefersDark: boolean,
): Theme {
  return parseTheme(chosen) ?? (systemPrefersDark ? "dark" : "light");
}

/** The `document.cookie` assignment that remembers a choice for a year. */
export function themeCookie(theme: Theme, secure: boolean): string {
  return `${THEME_COOKIE}=${theme}; Path=/; Max-Age=${ONE_YEAR_SECONDS}; SameSite=Lax${secure ? "; Secure" : ""}`;
}
