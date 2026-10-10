import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

// Runs the real `make web-node` target from the repository root, so the
// Makefile recipe and the guard script are exercised together.
const repoRoot = resolve(import.meta.dirname, "../../..");

function makeWebNode(...variables: string[]) {
  return spawnSync(
    "make",
    ["--no-print-directory", "-C", repoRoot, "web-node", ...variables],
    {
      encoding: "utf8",
    },
  );
}

test("make web-node accepts the Node version pinned in .nvmrc", () => {
  const result = makeWebNode();

  assert.equal(result.stderr, "");
  assert.equal(result.status, 0);
});

test("make web-node rejects a Node major version other than the pinned one", () => {
  const dir = mkdtempSync(join(tmpdir(), "web-node-"));
  try {
    const otherMajor = Number(process.versions.node.split(".")[0]) + 1;
    const versionFile = join(dir, ".nvmrc");
    writeFileSync(versionFile, `${otherMajor}.0.0\n`);

    const result = makeWebNode(`WEB_NODE_VERSION_FILE=${versionFile}`);

    assert.notEqual(result.status, 0);
    assert.match(
      result.stderr,
      new RegExp(
        `Node ${otherMajor}\\.0\\.0 required .*; found ${process.version}`,
      ),
    );
    assert.doesNotMatch(result.stderr, /SyntaxError/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
