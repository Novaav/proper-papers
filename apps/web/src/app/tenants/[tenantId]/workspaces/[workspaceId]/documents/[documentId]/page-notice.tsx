"use client";

import { useRouter } from "next/navigation.js";
import { useEffect, useState } from "react";

// How long after the page has loaded the message is put into its live region:
// long enough for screen readers to have registered the (empty) region and
// finished announcing the new page.
const ANNOUNCE_DELAY_MS = 500;

// How long a success message stays before it is dropped (see below).
const DISMISS_DELAY_MS = 6000;

// A fixed message about what the last form did (?processing=…, ?review=…).
// Those forms post and redirect, so the browser loads a new page, and screen
// readers do not announce text that is already inside a live region when the
// page loads: only changes are announced. So the region is rendered empty and
// the message is put into it shortly after load, without moving focus. Without
// JavaScript the <noscript> copy shows the message.
export function PageNotice({
  role,
  message,
}: {
  role: "status" | "alert";
  message: string;
}) {
  const router = useRouter();
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const show = setTimeout(() => setShown(true), ANNOUNCE_DELAY_MS);
    // A success message is a one-off about the form just sent. After a while it
    // is dropped from the address (a soft navigation to the same page), so it
    // does not linger beside a status that has moved on (for example
    // "Processing started." next to a finished run) or come back on a reload.
    // An alert is never dropped by itself: an error stays until the user acts.
    const drop =
      role === "status"
        ? setTimeout(() => {
            const url = new URL(window.location.href);
            if (
              !url.searchParams.has("processing") &&
              !url.searchParams.has("review")
            )
              return;
            url.searchParams.delete("processing");
            url.searchParams.delete("review");
            router.replace(`${url.pathname}${url.search}${url.hash}`, {
              scroll: false,
            });
          }, DISMISS_DELAY_MS)
        : undefined;
    return () => {
      clearTimeout(show);
      if (drop) clearTimeout(drop);
    };
  }, [role, router]);
  return (
    <>
      <p role={role} className="page-notice">
        {shown ? message : null}
      </p>
      <noscript>
        <p>{message}</p>
      </noscript>
    </>
  );
}
