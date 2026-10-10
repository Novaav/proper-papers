// Identifiers from URLs are untrusted. The API accepts only canonical
// lowercase UUIDs; anything else is rejected here before it reaches a URL.

const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function isCanonicalUuid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    CANONICAL_UUID.test(value) &&
    value !== "00000000-0000-0000-0000-000000000000"
  );
}
