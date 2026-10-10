import type { ReactNode } from "react";

import { Breadcrumbs, type Crumb } from "./breadcrumbs.tsx";

// The page-level bar: where you are on the left, this page's actions on the
// right. It stays at the top of the work surface while the page scrolls.
export function Toolbar({
  crumbs,
  children,
}: {
  crumbs: Crumb[];
  children?: ReactNode;
}) {
  return (
    <div className="toolbar">
      <Breadcrumbs items={crumbs} />
      {children ? <div className="toolbar-actions">{children}</div> : null}
    </div>
  );
}
