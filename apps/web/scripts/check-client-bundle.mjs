// After `next build`: fail if server-only authentication code or secrets
// appear in files served to the browser (.next/static).
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const STATIC_DIR = join(import.meta.dirname, "..", ".next", "static");
const FORBIDDEN = [
  "openid-client",
  "oauth4webapi",
  "iron-session",
  "iron-webcrypto",
  "OIDC_CLIENT_SECRET",
  "SESSION_SECRET",
  "client_secret",
  "code_verifier",
  "access_token",
  "accessToken",
  "__Host-session",
  "__Host-oidc",
];

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

let scanned = 0;
const hits = [];
for (const path of files(STATIC_DIR)) {
  if (!/\.(js|css|html|json|map|txt)$/.test(path)) continue;
  scanned += 1;
  const text = readFileSync(path, "utf8");
  for (const needle of FORBIDDEN) {
    if (text.includes(needle)) hits.push(`${path}: ${needle}`);
  }
}
if (scanned === 0) {
  console.error(
    "check-client-bundle: no files in .next/static; run next build first",
  );
  process.exit(1);
}
if (hits.length > 0) {
  console.error(
    `check-client-bundle: server-only material in the client bundle:\n${hits.join("\n")}`,
  );
  process.exit(1);
}
console.log(`check-client-bundle: ${scanned} client files clean`);
