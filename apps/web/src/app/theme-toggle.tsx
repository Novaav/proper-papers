"use client";

import {
  effectiveTheme,
  otherTheme,
  themeCookie,
  type Theme,
} from "@/lib/theme.ts";

// One button that switches between the dark and the light theme. Both states
// are in the server's HTML and CSS shows the one that matches the theme in
// effect (the visitor's choice on <html data-theme>, else the system
// preference), so the icon and the name are right from the first paint and
// nothing depends on client state. The sun is shown in dark mode (it switches to
// light), the moon in light mode. Clicking sets the attribute and remembers the
// choice in a cookie that the server reads on the next page. The button is
// always visible, including before hydration: navigation here is plain links
// (full page loads), so hiding it until JavaScript has run made it vanish on
// every page change. Before hydration a click does nothing, which is harmless.
export function ThemeToggle({ variant }: { variant: "rail" | "header" }) {
  const toggle = () => {
    const root = document.documentElement;
    const current: Theme = effectiveTheme(
      root.dataset.theme,
      window.matchMedia("(prefers-color-scheme: dark)").matches,
    );
    const next = otherTheme(current);
    root.dataset.theme = next;
    document.cookie = themeCookie(next, window.location.protocol === "https:");
  };
  return (
    <button
      type="button"
      className={`theme-toggle theme-toggle--${variant}`}
      onClick={toggle}
    >
      <span className="theme-state theme-state--dark">
        <SunIcon />
        <span className="theme-label">Switch to light theme</span>
      </span>
      <span className="theme-state theme-state--light">
        <MoonIcon />
        <span className="theme-label">Switch to dark theme</span>
      </span>
    </button>
  );
}

function SunIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="8" cy="8" r="2.7" />
      <path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8a5.6 5.6 0 1 0 6.8 6.8Z" />
    </svg>
  );
}
