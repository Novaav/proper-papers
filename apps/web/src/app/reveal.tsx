"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

// Reveals a block of the landing page once, as it first scrolls into view.
// Without JavaScript (or with reduced motion) the content is simply there: the
// hidden state is only ever set by this effect, and only for blocks that start
// below the fold, so nothing flashes and nothing moves the layout (opacity and
// a few pixels of transform only). Each block reveals once and is then left
// alone.
export function Reveal({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pending, setPending] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (
      window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
      !("IntersectionObserver" in window) ||
      element.getBoundingClientRect().top < window.innerHeight
    ) {
      return;
    }
    setPending(true);
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setPending(false);
          observer.disconnect();
        }
      },
      { rootMargin: "0px 0px -12% 0px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return (
    <div ref={ref} className={pending ? "reveal reveal--pending" : "reveal"}>
      {children}
    </div>
  );
}
