import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import {
  initialUploadState,
  uploadReducer,
  type UploadEvent,
} from "./upload-state.ts";

const run = (hasError: boolean, events: UploadEvent[]) =>
  events.reduce(uploadReducer, initialUploadState(hasError));

test("an error from the previous upload is visible on load", () => {
  assert.deepEqual(initialUploadState(true), {
    uploading: false,
    errorVisible: true,
    requiredFailed: false,
  });
  assert.deepEqual(initialUploadState(false), {
    uploading: false,
    errorVisible: false,
    requiredFailed: false,
  });
});

test("the error stays until the user acts: page_shown never hides it", () => {
  assert.equal(run(true, ["page_shown", "page_shown"]).errorVisible, true);
});

test("choosing another file hides the error without starting an upload", () => {
  assert.deepEqual(run(true, ["file_changed"]), {
    uploading: false,
    errorVisible: false,
    requiredFailed: false,
  });
});

test("submitting hides the stale error and shows progress", () => {
  assert.deepEqual(run(true, ["submitted"]), {
    uploading: true,
    errorVisible: false,
    requiredFailed: false,
  });
});

test("a page restored from history accepts a new upload", () => {
  assert.equal(run(false, ["submitted", "page_shown"]).uploading, false);
  // A restored page keeps its message as it was shown.
  assert.equal(run(true, ["page_shown"]).errorVisible, true);
});

test("initial state is never invalid without an error (VoiceOver defect)", () => {
  const state = initialUploadState(false);
  assert.equal(state.errorVisible || state.requiredFailed, false);
  assert.equal(run(false, ["page_shown"]).requiredFailed, false);
});

test("a submit refused by the browser's required check marks the field invalid", () => {
  const state = run(false, ["validation_failed"]);
  assert.deepEqual(state, {
    uploading: false,
    errorVisible: false,
    requiredFailed: true,
  });
  // Choosing a file clears it; so does a submit that goes through.
  assert.equal(
    run(false, ["validation_failed", "file_changed"]).requiredFailed,
    false,
  );
  assert.equal(
    run(false, ["validation_failed", "submitted"]).requiredFailed,
    false,
  );
});

test("no timers anywhere in the upload feedback", () => {
  for (const name of ["upload-form.tsx", "upload-state.ts", "page.tsx"]) {
    const code = readFileSync(join(import.meta.dirname, name), "utf8").replace(
      /\/\*[\s\S]*?\*\/|\/\/.*$/gm,
      "",
    );
    assert.equal(
      /setTimeout|setInterval|requestAnimationFrame|requestIdleCallback/.test(
        code,
      ),
      false,
      name,
    );
  }
});

test("the form's error is an alert tied to the file field", () => {
  const form = readFileSync(
    join(import.meta.dirname, "upload-form.tsx"),
    "utf8",
  );
  assert.match(form, /id="document-file-error" role="alert"/);
  assert.match(form, /"document-file-hint document-file-error"/);
  // Always explicit: `required` alone makes browsers expose the empty field as
  // invalid before any interaction (found with VoiceOver in the live check).
  assert.match(form, /const invalid = showError \|\| state\.requiredFailed;/);
  assert.match(form, /aria-invalid=\{invalid\}/);
  assert.match(form, /onInvalid=\{\(\) => dispatch\("validation_failed"\)\}/);
  assert.match(form, /\srequired\s/);
  assert.match(form, /onChange=\{\(\) => dispatch\("file_changed"\)\}/);
  assert.match(form, /onSubmit=\{\(\) => dispatch\("submitted"\)\}/);
});
