// Per-request memoised API reads. The signed-in frame (the navigation rail)
// and the page it wraps both need the same answer; React's cache() makes them
// share one API call within a single render instead of calling the API twice.
// Same functions, same authorization: the API still decides on every call.
import { cache } from "react";

import { getMe } from "./me.ts";
import { listWorkspaces } from "./workspaces.ts";

export const getMeOnce = cache((baseUrl: string | undefined, token: string) =>
  getMe(baseUrl, token),
);

export const listWorkspacesOnce = cache(
  (baseUrl: string | undefined, token: string, tenantId: string) =>
    listWorkspaces(baseUrl, token, tenantId),
);
