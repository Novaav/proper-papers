"use client";

import { useEffect, useState } from "react";

// The review workbench is plain links and panels: choosing a field is a link to
// its panel (#field-…), and CSS shows the panel the address points at (or the
// default one), so it works without JavaScript. This adds what CSS cannot say:
// which field is current (aria-current on its link), a spoken "Showing …" when
// the panel changes, and Up/Down arrows between the field links. Two more
// things around a review decision, which is a plain form post: while it is
// being sent its buttons are disabled and "Saving decision…" is spoken, so it
// cannot be sent twice; and when the page comes back from a saved decision
// (?review=decided) focus goes to the field it moved on to (or to the approval
// once the last field is decided), so keyboard and screen-reader users continue
// where the review continues. It changes no data and sends nothing itself.
export function WorkbenchSync({ labels }: { labels: Record<string, string> }) {
  const [message, setMessage] = useState("");

  useEffect(() => {
    const root = document.querySelector<HTMLElement>(".workbench");
    if (!root) return;
    const panes = [...root.querySelectorAll<HTMLElement>(".pane")];
    const links = [...root.querySelectorAll<HTMLAnchorElement>(".field-link")];
    const fallback = root.dataset.default ?? panes[0]?.id ?? "";

    const apply = (announce: boolean) => {
      const wanted = window.location.hash.slice(1);
      const id = panes.some((pane) => pane.id === wanted) ? wanted : fallback;
      for (const link of links) {
        if (link.getAttribute("href") === `#${id}`) {
          link.setAttribute("aria-current", "true");
        } else {
          link.removeAttribute("aria-current");
        }
      }
      if (announce && labels[id]) setMessage(`Showing ${labels[id]}`);
    };
    apply(false);

    const onHash = () => apply(true);
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const from = links.indexOf(event.target as HTMLAnchorElement);
      if (from < 0) return;
      const next = links[from + (event.key === "ArrowDown" ? 1 : -1)];
      if (next) {
        event.preventDefault();
        next.focus();
      }
    };
    const forms = [
      ...document.querySelectorAll<HTMLFormElement>(".review-action"),
    ];
    const buttons = () =>
      document.querySelectorAll<HTMLButtonElement>(".review-action button");
    const onSubmit = () => {
      setMessage("Saving decision…");
      root.setAttribute("aria-busy", "true");
      // After the form data is taken: a disabled submitter is not sent.
      setTimeout(() => {
        for (const button of buttons()) button.disabled = true;
      }, 0);
    };
    const onShow = (event: PageTransitionEvent) => {
      // Back to this page from the browser's cache: the decision was not sent here.
      if (!event.persisted) return;
      root.removeAttribute("aria-busy");
      for (const button of buttons()) button.disabled = false;
    };
    for (const form of forms) form.addEventListener("submit", onSubmit);
    window.addEventListener("pageshow", onShow);

    // Back from a saved decision: continue where the review continues.
    const params = new URLSearchParams(window.location.search);
    let focusTimer: ReturnType<typeof setTimeout> | undefined;
    if (params.get("review") === "decided") {
      const target = window.location.hash.slice(1);
      const heading =
        target === "approval"
          ? document.getElementById("approval")
          : (panes
              .find((pane) => pane.id === target)
              ?.querySelector<HTMLElement>(".pane-value") ?? null);
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        focusTimer = setTimeout(() => heading.focus(), 50);
      }
    }

    window.addEventListener("hashchange", onHash);
    root.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("hashchange", onHash);
      root.removeEventListener("keydown", onKey);
      window.removeEventListener("pageshow", onShow);
      for (const form of forms) form.removeEventListener("submit", onSubmit);
      if (focusTimer) clearTimeout(focusTimer);
    };
  }, [labels]);

  return (
    <p className="visually-hidden" role="status">
      {message}
    </p>
  );
}
