// Exits non-zero when the running Node major version differs from the pinned
// version file. Plain JavaScript on purpose: it must run, and fail clearly, on
// the wrong Node version.
import { readFileSync } from "node:fs";

const versionFile = process.argv[2];
if (!versionFile) {
  console.error("usage: node check-node-version.mjs <version-file>");
  process.exit(2);
}

const wanted = readFileSync(versionFile, "utf8").trim().replace(/^v/, "");
if (process.versions.node.split(".")[0] !== wanted.split(".")[0]) {
  console.error(
    `Node ${wanted} required (see ${versionFile}); found ${process.version}`,
  );
  process.exit(1);
}
