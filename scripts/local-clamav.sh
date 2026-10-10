#!/usr/bin/env bash
# Local ClamAV (clamd) for development and live checks, via Docker.
#
#   scripts/local-clamav.sh up       create (first run) or start the container, update
#                                    the signatures once, and wait until clamd answers
#   scripts/local-clamav.sh update   download current official signatures (freshclam) and
#                                    reload clamd; the API refuses signatures older than 72 h
#   scripts/local-clamav.sh status   container state, PING, and the signature date
#   scripts/local-clamav.sh down     stop the container (kept; `up` starts it again)
#   scripts/local-clamav.sh remove   delete this project's container (only if labelled ours)
#
# It runs the exact digest-pinned image that CI uses (read from
# .github/workflows/ci.yml, so there is one source of truth) with the test
# configuration from apps/api/tests/clamav/ (AlertEncrypted, AlertExceedsMax, the
# limits) and the test-only marker signature, which exists for local checks only.
# clamd is published on 127.0.0.1:3310 only. The script never installs
# anything, never touches Homebrew or system services, and only ever acts on
# the one container named below that carries the label below.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME=production-ai-flagship-clamd
LABEL=org.production-ai-flagship.role=local-clamd
PORT=3310
# The pinned image is published for linux/amd64 only, so it is requested
# explicitly: on Apple Silicon Docker then runs it under emulation instead of
# failing to find an arm64 variant (and DOCKER_DEFAULT_PLATFORM is not needed).
# Revisit together with the digest if an upgrade brings a native arm64 image.
PLATFORM=linux/amd64
CONF_DIR="$ROOT/apps/api/tests/clamav"
IMAGE="$(sed -n 's/^ *CLAMAV_IMAGE: *\(clamav\/clamav:[^ ]*@sha256:[0-9a-f]\{64\}\) *$/\1/p' \
  "$ROOT/.github/workflows/ci.yml")"

fail() {
  echo "local-clamav: $*" >&2
  exit 1
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    cat >&2 <<'EOF'
local-clamav: Docker is not installed. Nothing was changed.
Install Docker (for example Docker Desktop) yourself if you want a local
scanner, start it, and run this command again. Uploads without a scanner are
refused by the API (503 scan_unavailable); the CI real-engine tests do not
need a local scanner.
EOF
    exit 3
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "local-clamav: Docker is installed but not running. Start it and retry. Nothing was changed." >&2
    exit 3
  fi
  [ -n "$IMAGE" ] || fail "could not read the pinned CLAMAV_IMAGE from .github/workflows/ci.yml"
}

# "running", "exited", ... or "" when the container does not exist.
state() {
  docker inspect -f '{{.State.Status}}' "$NAME" 2>/dev/null || true
}

require_ours() {
  local label
  label="$(docker inspect -f '{{index .Config.Labels "org.production-ai-flagship.role"}}' "$NAME")"
  [ "$label" = "local-clamd" ] || fail "a container named $NAME exists but is not this project's; not touching it"
}

# Send one NUL-terminated clamd command to 127.0.0.1:PORT; print the reply.
clamd_command() {
  python3 - "$PORT" "$1" <<'PY'
import socket, sys
port, command = int(sys.argv[1]), sys.argv[2].encode()
try:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"z" + command + b"\0")
        data = b""
        while not data.endswith(b"\0"):
            chunk = sock.recv(1024)
            if not chunk:
                break
            data += chunk
except OSError:
    sys.exit(1)
print(data.rstrip(b"\0").decode("ascii", "replace"))
PY
}

wait_for_pong() {
  for _ in $(seq 1 150); do
    if [ "$(clamd_command PING 2>/dev/null || true)" = "PONG" ]; then
      return 0
    fi
    sleep 2
  done
  docker logs --tail 40 "$NAME" >&2 || true
  fail "clamd did not answer on 127.0.0.1:$PORT"
}

# The database version from VERSION ("ClamAV 1.4.6/28139/<date>" -> "28139").
database_version() {
  clamd_command VERSION 2>/dev/null | sed -n 's/^ClamAV [^/]*\/\([0-9]*\)\/.*$/\1/p'
}

update() {
  echo "local-clamav: downloading current official signatures (freshclam)..."
  local before output
  before="$(database_version)"
  if ! output="$(docker exec "$NAME" freshclam --stdout 2>&1)"; then
    echo "local-clamav: freshclam failed; signatures may be stale (the API refuses signatures older than 72 h)" >&2
    return 0
  fi
  if ! grep -q "updated" <<<"$output"; then
    echo "local-clamav: signatures already current"
    return 0
  fi
  [ "$(clamd_command RELOAD || true)" = "RELOADING" ] || echo "local-clamav: clamd did not confirm the reload" >&2
  # clamd reloads in the background and keeps answering meanwhile: wait until
  # it reports the new database, not just until it answers.
  for _ in $(seq 1 90); do
    if [ -n "$(database_version)" ] && [ "$(database_version)" != "$before" ]; then
      echo "local-clamav: signatures updated (database $before -> $(database_version))"
      return 0
    fi
    sleep 2
  done
  echo "local-clamav: clamd still reports database $before after the update; check 'status' again" >&2
}

status() {
  local current
  current="$(state)"
  if [ -z "$current" ]; then
    echo "local-clamav: no container (run: scripts/local-clamav.sh up)"
    return 0
  fi
  require_ours
  echo "local-clamav: container $NAME is $current; image $(docker inspect -f '{{.Config.Image}}' "$NAME")"
  if [ "$current" = "running" ]; then
    echo "local-clamav: PING -> $(clamd_command PING || echo 'no answer')"
    echo "local-clamav: VERSION -> $(clamd_command VERSION || echo 'no answer')"
  fi
}

cmd="${1:-}"
case "$cmd" in
  up)
    require_docker
    current="$(state)"
    if [ -z "$current" ]; then
      docker create --platform "$PLATFORM" --name "$NAME" --label "$LABEL" -p "127.0.0.1:$PORT:3310" \
        -e CLAMAV_NO_FRESHCLAMD=true -e CLAMAV_NO_MILTERD=true "$IMAGE" >/dev/null
      # Copied, not bind-mounted: the image's entrypoint chowns these paths.
      docker cp "$CONF_DIR/clamd.conf" "$NAME:/etc/clamav/clamd.conf"
      docker cp "$CONF_DIR/test.ndb" "$NAME:/var/lib/clamav/test.ndb"
      docker start "$NAME" >/dev/null
    else
      require_ours
      [ "$current" = "running" ] || docker start "$NAME" >/dev/null
    fi
    wait_for_pong
    update
    status
    ;;
  update)
    require_docker
    [ "$(state)" = "running" ] || fail "the container is not running (run: scripts/local-clamav.sh up)"
    require_ours
    update
    ;;
  status)
    require_docker
    status
    ;;
  down)
    require_docker
    current="$(state)"
    if [ -n "$current" ]; then
      require_ours
      [ "$current" != "running" ] || docker stop "$NAME" >/dev/null
    fi
    echo "local-clamav: stopped"
    ;;
  remove)
    require_docker
    if [ -n "$(state)" ]; then
      require_ours
      docker rm -f "$NAME" >/dev/null
    fi
    echo "local-clamav: removed"
    ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
