// State of the upload form after a page load. Feedback never changes on its own
// (no timers): an error from the previous upload stays visible until the user
// acts, by choosing another file or submitting again. The field is marked
// invalid only after an actual failure: a previous upload error, or the
// browser's own required check refusing an empty submit.

export interface UploadState {
  uploading: boolean;
  errorVisible: boolean;
  /** The browser's required check refused a submit without a file. */
  requiredFailed: boolean;
}

export type UploadEvent =
  /** The user chose (or cleared) a file. */
  | "file_changed"
  /** The form is being submitted. */
  | "submitted"
  /** The browser's own validation refused the submit (no file chosen). */
  | "validation_failed"
  /** The page was shown again, e.g. restored from the back/forward cache. */
  | "page_shown";

export function initialUploadState(hasError: boolean): UploadState {
  return { uploading: false, errorVisible: hasError, requiredFailed: false };
}

export function uploadReducer(
  state: UploadState,
  event: UploadEvent,
): UploadState {
  switch (event) {
    case "file_changed":
      return { ...state, errorVisible: false, requiredFailed: false };
    case "submitted":
      return { uploading: true, errorVisible: false, requiredFailed: false };
    case "validation_failed":
      return { ...state, requiredFailed: true };
    case "page_shown":
      // A restored page must accept a new upload; its message stays as it was.
      return { ...state, uploading: false };
  }
}
