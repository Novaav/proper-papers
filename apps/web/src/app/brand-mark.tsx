// The product mark: a document page with one highlighted line, which is what
// the product does: it shows the exact source text a value came from. Drawn
// with classes (no inline styles under the CSP) so it follows the colour scheme.
export function BrandMark() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect className="brand-tile" width="32" height="32" rx="8" />
      <rect
        className="brand-hl"
        x="7"
        y="14"
        width="18"
        height="5.5"
        rx="1.5"
      />
      <path
        className="brand-line"
        d="M9.5 9.5h13M9.5 22.5h8"
        strokeWidth="1.8"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}
