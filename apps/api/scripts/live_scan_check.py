"""End-to-end malware-scanning checks against a running API (development only).

    make api-live-scan-check

Talks to the API over HTTP as a real client (no shortcuts, no weakened
authentication) with a member's access token, and checks, in order:
  1. /health/scanner is 200;
  2. a clean synthetic PDF is stored as clean and downloads byte-identical;
  3. a PDF with the test-only marker signature is refused (422 malware_detected),
     and nothing is stored;
  4. an encrypted synthetic PDF is refused (422 encrypted_pdf), nothing stored;
  5. documents stored before scanning (not_scanned) cannot be downloaded (409);
  6. another tenant's workspace is 404 for listing and uploading;
  7. optionally (LIVE_SCANNER_OUTAGE=1, local Docker scanner only): with the
     scanner stopped, /health/scanner is 503 and an upload is 503
     scan_unavailable with nothing stored; the scanner is started again.

Settings (environment; the token is never printed):
  LIVE_ACCESS_TOKEN          a member's access token (required)
  LIVE_TENANT_ID             the member's tenant (required)
  LIVE_WORKSPACE_ID          a workspace in it (required)
  LIVE_FOREIGN_TENANT_ID     a tenant the member does not belong to (required)
  LIVE_FOREIGN_WORKSPACE_ID  a workspace in that tenant (required)
  LIVE_API_BASE_URL          default http://127.0.0.1:8000
  LIVE_SCANNER_OUTAGE        "1" to include check 7

Only synthetic files are uploaded. The one clean upload stays (it is a
development record like any other); nothing is ever deleted: the script sends
only GET and POST requests. It stops at the first failed check.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from tests import synthetic_pdfs

ROOT = Path(__file__).resolve().parents[3]
REQUIRED = (
    "LIVE_ACCESS_TOKEN",
    "LIVE_TENANT_ID",
    "LIVE_WORKSPACE_ID",
    "LIVE_FOREIGN_TENANT_ID",
    "LIVE_FOREIGN_WORKSPACE_ID",
)


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes

    def json(self) -> object:
        try:
            return json.loads(self.body)
        except ValueError:
            return None


# (method, path, headers, body) -> Reply. Only GET and POST are ever used.
Send = Callable[[str, str, dict[str, str], bytes | None], Reply]


class CheckFailed(Exception):
    pass


def http_sender(base_url: str) -> Send:
    def send(method: str, path: str, headers: dict[str, str], body: bytes | None) -> Reply:
        assert method in ("GET", "POST")
        request = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return Reply(response.status, response.read())
        except urllib.error.HTTPError as error:
            return Reply(error.code, error.read())
        except OSError as error:
            raise CheckFailed(
                f"cannot reach the API at {base_url}: {type(error).__name__}"
            ) from None

    return send


def run(settings: Mapping[str, str], send: Send, out: Callable[[str], None] = print) -> None:
    """Run the checks; raises CheckFailed at the first failure."""
    auth = {"Authorization": f"Bearer {settings['LIVE_ACCESS_TOKEN']}"}
    tenant, workspace = settings["LIVE_TENANT_ID"], settings["LIVE_WORKSPACE_ID"]
    foreign = (settings["LIVE_FOREIGN_TENANT_ID"], settings["LIVE_FOREIGN_WORKSPACE_ID"])
    docs = f"/v1/tenants/{tenant}/workspaces/{workspace}/documents"

    def check(label: str, condition: bool, detail: str = "") -> None:
        if not condition:
            raise CheckFailed(f"{label}{': ' + detail if detail else ''}")
        out(f"  [PASS] {label}")

    def listed() -> list[dict[str, object]]:
        reply = send("GET", docs, auth, None)
        body = reply.json()
        if reply.status != 200 or not isinstance(body, dict):
            raise CheckFailed(f"listing documents: HTTP {reply.status}")
        documents = body.get("documents")
        if not isinstance(documents, list):
            raise CheckFailed("listing documents: unexpected body")
        return documents

    def upload(path: str, content: bytes, name: str) -> Reply:
        headers = {
            **auth,
            "Content-Type": "application/pdf",
            "X-Document-Name": quote(name, safe=""),
        }
        return send("POST", path, headers, content)

    stamp = time.strftime("%Y%m%dT%H%M%S")
    out("live scan check:")

    health = send("GET", "/health/scanner", {}, None)
    check("1. /health/scanner is 200", health.status == 200, f"HTTP {health.status}")

    before = listed()
    clean = synthetic_pdfs.clean_pdf()
    stored = upload(docs, clean, f"live-scan-check-clean-{stamp}.pdf")
    body = stored.json()
    check(
        "2a. a clean synthetic PDF is stored (201)", stored.status == 201, f"HTTP {stored.status}"
    )
    assert isinstance(body, dict)
    version = body.get("version")
    check(
        "2b. it is listed as clean",
        isinstance(version, dict) and version.get("scan_status") == "clean",
    )
    content = send("GET", f"{docs}/{body['id']}/content", auth, None)
    check(
        "2c. it downloads byte-identical",
        content.status == 200
        and hashlib.sha256(content.body).digest() == hashlib.sha256(clean).digest(),
        f"HTTP {content.status}",
    )
    count = len(listed())
    check(
        "2d. exactly one document was added", count == len(before) + 1, f"{len(before)} -> {count}"
    )

    for label, pdf, detail in (
        ("3. the test-signature PDF", synthetic_pdfs.marker_pdf(), "malware_detected"),
        (
            "4. an encrypted PDF",
            synthetic_pdfs.encrypted_pdf(user_password=b"secret"),
            "encrypted_pdf",
        ),
    ):
        refused = upload(docs, pdf, f"live-scan-check-{detail}-{stamp}.pdf")
        check(
            f"{label} is refused (422 {detail})",
            (refused.status, refused.json()) == (422, {"detail": detail}),
            f"HTTP {refused.status} {refused.body[:80]!r}",
        )
        check(f"{label}: nothing was stored", len(listed()) == count)

    legacy = [d for d in listed() if _scan_status(d) == "not_scanned"]
    if not legacy:
        out("  [SKIP] 5. no documents from before scanning in this workspace")
    for document in legacy:
        refused = send("GET", f"{docs}/{document['id']}/content", auth, None)
        check(
            "5. a document from before scanning cannot be downloaded (409 not_scanned)",
            (refused.status, refused.json()) == (409, {"detail": "not_scanned"}),
            f"HTTP {refused.status}",
        )

    foreign_docs = f"/v1/tenants/{foreign[0]}/workspaces/{foreign[1]}/documents"
    check(
        "6a. another tenant's documents are 404",
        send("GET", foreign_docs, auth, None).status == 404,
    )
    check(
        "6b. an upload to another tenant is 404",
        upload(foreign_docs, clean, f"live-scan-check-foreign-{stamp}.pdf").status == 404,
    )
    check("6c. nothing was stored", len(listed()) == count)

    if settings.get("LIVE_SCANNER_OUTAGE") == "1":
        scanner("down")
        try:
            health = send("GET", "/health/scanner", {}, None)
            check("7a. with the scanner stopped, /health/scanner is 503", health.status == 503)
            refused = upload(docs, clean, f"live-scan-check-outage-{stamp}.pdf")
            check(
                "7b. an upload is refused (503 scan_unavailable)",
                (refused.status, refused.json()) == (503, {"detail": "scan_unavailable"}),
                f"HTTP {refused.status}",
            )
            check("7c. nothing was stored", len(listed()) == count)
        finally:
            scanner("up")
    out("live scan check: all checks passed")


def _scan_status(document: dict[str, object]) -> object:
    version = document.get("version")
    return version.get("scan_status") if isinstance(version, dict) else None


def scanner(command: str) -> None:
    """Stop or start this project's local Docker scanner (scripts/local-clamav.sh)."""
    subprocess.run([str(ROOT / "scripts" / "local-clamav.sh"), command], check=True)


def main(environ: Mapping[str, str] = os.environ) -> int:
    missing = [name for name in REQUIRED if not environ.get(name)]
    if missing:
        print(f"live scan check: missing settings: {', '.join(missing)}", file=sys.stderr)
        return 2
    base_url = environ.get("LIVE_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        run(environ, http_sender(base_url))
    except CheckFailed as failure:
        print(f"  [FAIL] {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
