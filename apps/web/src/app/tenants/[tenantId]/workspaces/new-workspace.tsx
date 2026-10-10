"use client";

import { useState } from "react";

import { Popover } from "../../../popover.tsx";

// "New workspace": a toolbar action that opens the create form. The form posts
// to the same route as before; with JavaScript it also says "Creating…" while
// the request is sent. Nothing is created in the browser: the API creates the
// workspace and the page that follows lists what the API returns.
export function NewWorkspace({
  action,
  maxLength,
  open,
  nameError,
  errorText,
}: {
  action: string;
  maxLength: number;
  open: boolean;
  nameError: boolean;
  errorText: string;
}) {
  const [sending, setSending] = useState(false);
  return (
    <Popover
      className="new-workspace"
      open={open}
      focusSelector="#workspace-name"
      label={
        <>
          <PlusIcon />
          New workspace
        </>
      }
    >
      <section aria-labelledby="create-workspace">
        <h2 id="create-workspace">Create a workspace</h2>
        <form method="post" action={action} onSubmit={() => setSending(true)}>
          <label htmlFor="workspace-name">Name</label>
          <input
            id="workspace-name"
            name="name"
            type="text"
            required
            maxLength={maxLength}
            autoComplete="off"
            aria-describedby={
              nameError
                ? "workspace-name-hint workspace-name-error"
                : "workspace-name-hint"
            }
            aria-invalid={nameError || undefined}
          />
          <p id="workspace-name-hint" className="hint">
            1 to {maxLength} characters.
          </p>
          {nameError && (
            <p id="workspace-name-error" role="alert" className="error">
              {errorText}
            </p>
          )}
          <button
            type="submit"
            className="button-primary"
            disabled={sending}
            aria-disabled={sending}
          >
            {sending ? "Creating…" : "Create workspace"}
          </button>
        </form>
      </section>
    </Popover>
  );
}

export function PlusIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M8 3v10M3 8h10" />
    </svg>
  );
}
