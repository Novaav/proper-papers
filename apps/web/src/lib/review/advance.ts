// Where a saved review decision takes the reviewer. The document page puts the
// answer in each decision form (`after`) and the review route follows it only
// once the API has saved the decision; a refused or failed decision never
// advances, and nothing here approves anything.

/** The part of a field row this needs. */
interface Row {
  id: string;
  key: string;
}

/**
 * For a decision on `row`: the key of the next field that still needs a
 * decision (in record order, wrapping round, the same order as the "Next to
 * review" link), or "approval" when no other field is pending. That holds
 * whether `row` itself was pending or was already decided (a changed mind):
 * once the decision is saved nothing is left to decide, so the review goes to
 * the approval panel, which says whether approving is possible and, if not,
 * why. "stay" is no longer produced; the review route still accepts it.
 */
export function afterDecision(row: Row, rows: Row[], needs: Row[]): string {
  const others = needs.filter((candidate) => candidate.id !== row.id);
  if (others.length > 0) {
    const index = rows.findIndex((candidate) => candidate.id === row.id);
    const target =
      others.find((candidate) => rows.indexOf(candidate) > index) ?? others[0];
    if (target) return target.key;
  }
  return "approval";
}
