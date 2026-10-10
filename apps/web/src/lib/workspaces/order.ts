// Presentation only: workspaces newest first, whatever order the API lists
// them in (it lists oldest first). Ties keep the API's relative order. The
// list the API returns is never changed or filtered here.
export function newestFirst<T extends { createdAt: string }>(
  workspaces: readonly T[],
): T[] {
  return workspaces
    .map((workspace, index) => ({ workspace, index }))
    .sort((a, b) => {
      const byTime =
        Date.parse(b.workspace.createdAt) - Date.parse(a.workspace.createdAt);
      return Number.isNaN(byTime) || byTime === 0 ? a.index - b.index : byTime;
    })
    .map(({ workspace }) => workspace);
}
