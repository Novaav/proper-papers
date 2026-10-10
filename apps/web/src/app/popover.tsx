"use client";

import { useRef, type ReactNode } from "react";

// A toolbar action that opens a small panel. It is a native disclosure
// (<details>), so it opens and closes without JavaScript. With JavaScript it
// also moves focus into the panel's first field when it opens, closes on
// Escape and returns focus to its button. Nothing is stored here: the forms
// inside post to the server like any other form.
export function Popover({
  label,
  className,
  open,
  focusSelector,
  children,
}: {
  label: ReactNode;
  className?: string;
  /** Open on load, e.g. after the server refused what was sent. */
  open: boolean;
  /** The field to focus when the panel opens. */
  focusSelector: string;
  children: ReactNode;
}) {
  const details = useRef<HTMLDetailsElement>(null);
  return (
    <details
      ref={details}
      className={className ? `popover ${className}` : "popover"}
      open={open || undefined}
      onToggle={() => {
        if (details.current?.open) {
          details.current.querySelector<HTMLElement>(focusSelector)?.focus();
        }
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && details.current?.open) {
          details.current.open = false;
          details.current.querySelector("summary")?.focus();
        }
      }}
    >
      <summary className="button-primary">{label}</summary>
      <div className="popover-panel">{children}</div>
    </details>
  );
}
