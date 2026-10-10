// Where the page sits, with a visible way back: every crumb but the last is a
// link, and the nearest parent carries a back arrow. The last crumb names the
// current page (aria-current) and is not a link.
export interface Crumb {
  label: string;
  href?: string;
}

export function Breadcrumbs({ items }: { items: Crumb[] }) {
  const parent = items.length >= 2 ? items[items.length - 2] : undefined;
  return (
    <nav aria-label="Breadcrumb" className="breadcrumbs">
      {parent?.href && (
        <a
          href={parent.href}
          className="crumb-back"
          aria-label={`Back to ${parent.label}`}
        >
          <BackArrow />
        </a>
      )}
      <ol>
        {items.map((item, index) => (
          <li key={index}>
            {item.href && index < items.length - 1 ? (
              <a href={item.href}>{item.label}</a>
            ) : (
              <span aria-current="page">{item.label}</span>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}

function BackArrow() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M9.5 3.5L5 8l4.5 4.5" />
    </svg>
  );
}
