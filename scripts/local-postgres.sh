#!/usr/bin/env bash
# Disposable local PostgreSQL 18 cluster for development.
#
#   scripts/local-postgres.sh install   build PostgreSQL from verified source into .local/
#   scripts/local-postgres.sh up        create (first run) and start the cluster
#   scripts/local-postgres.sh down      stop the cluster (data is kept)
#   scripts/local-postgres.sh test-db   recreate the disposable test database app_test
#   scripts/local-postgres.sh operator-role
#                                       add the app_operator role to an existing cluster
#                                       (idempotent; test-db runs it too)
#   scripts/local-postgres.sh worker-role
#                                       add the app_worker role to an existing cluster
#                                       (idempotent; test-db runs it too; no grants on
#                                       the development database)
#   scripts/local-postgres.sh demo-db   create the disposable demo database app_demo if it
#                                       is missing (an existing one is kept) and write its
#                                       URLs to demo.env
#   scripts/local-postgres.sh demo-reset
#                                       drop and recreate app_demo (all demo data is lost)
#   scripts/local-postgres.sh demo-drop drop app_demo and remove demo.env
#   scripts/local-postgres.sh demo-check
#                                       fail unless app_demo exists with its marker and
#                                       every URL in demo.env names app_demo
#
# Everything lives under .local/postgres/ (gitignored). The cluster listens on
# 127.0.0.1:55432 only, with its Unix socket inside .local/. Every command
# targets that data directory and port explicitly and never touches any other
# PostgreSQL installation. The only things ever deleted are the test database
# app_test and the demo database app_demo, each only when it carries its
# disposable marker (the database comment). The development database app is
# never created, changed or dropped by the demo commands.
set -euo pipefail

PG_VERSION=18.6
PG_SHA256=555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f
PORT=55432
DB_NAME=app
TEST_DB_NAME=app_test
TEST_DB_MARKER=disposable-test-database
DEMO_DB_NAME=app_demo
DEMO_DB_MARKER=disposable-demo-database

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="$ROOT/.local/postgres"
PREFIX="$BASE/$PG_VERSION"
BIN="$PREFIX/bin"
DATA="$BASE/data"
STAGING="$BASE/data.initializing"
RUN="$BASE/run"
LOG="$BASE/postgres.log"
CREDENTIALS="$BASE/credentials.env"
TEST_ENV="$BASE/test.env"
DEMO_ENV="$BASE/demo.env"
MARKER="$DATA/.local-dev-cluster"
ROLES_SQL="$ROOT/apps/api/db/roles.sql"
DATABASE_SQL="$ROOT/apps/api/db/database.sql"

# Ignore any libpq environment that could redirect commands to another server.
unset PGHOST PGHOSTADDR PGPORT PGUSER PGDATABASE PGPASSWORD PGPASSFILE PGSERVICE PGSERVICEFILE PGDATA

die() { echo "local-postgres: $*" >&2; exit 1; }

require_binaries() {
  [[ -x "$BIN/pg_ctl" ]] || die "PostgreSQL $PG_VERSION not found in $PREFIX; run 'make api-db-install' first"
  local version
  version="$("$BIN/postgres" --version)"
  [[ "$version" == *" $PG_VERSION"* ]] || die "unexpected server binary: $version"
}

port_in_use() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; }

random_password() { openssl rand -hex 24; }

cmd_install() {
  if [[ -x "$BIN/postgres" ]]; then
    echo "PostgreSQL $PG_VERSION already installed in $PREFIX"
    return
  fi
  local src="$BASE/src"
  local tarball="$src/postgresql-$PG_VERSION.tar.bz2"
  mkdir -p "$src"
  curl -fsSL -o "$tarball" "https://ftp.postgresql.org/pub/source/v$PG_VERSION/postgresql-$PG_VERSION.tar.bz2"
  echo "$PG_SHA256  $tarball" | shasum -a 256 -c - >/dev/null || die "checksum mismatch for $tarball"
  tar -xjf "$tarball" -C "$src"
  (
    cd "$src/postgresql-$PG_VERSION"
    ./configure --prefix="$PREFIX" --without-icu --with-libedit-preferred >"$BASE/build.log" 2>&1
    make -j"$(sysctl -n hw.ncpu)" >>"$BASE/build.log" 2>&1
    make install >>"$BASE/build.log" 2>&1
  ) || die "build failed; see $BASE/build.log"
  echo "Installed $("$BIN/postgres" --version) into $PREFIX"
}

server_opts() {
  echo "-c listen_addresses=127.0.0.1 -c port=$PORT -c unix_socket_directories=$RUN -c unix_socket_permissions=0700"
}

cmd_up() {
  require_binaries
  # macOS limits Unix socket paths to 103 bytes.
  local socket="$RUN/.s.PGSQL.$PORT"
  ((${#socket} <= 103)) || die "socket path too long (${#socket} > 103 bytes): $socket; use a shorter checkout path"
  [[ -e "$STAGING" ]] && die "incomplete initialization found at $STAGING; inspect and remove it manually"

  if [[ ! -d "$DATA" ]]; then
    initialize
  fi

  [[ -f "$MARKER" ]] || die "$DATA exists but is not a cluster created by this script; refusing to use it"
  [[ "$(cat "$DATA/PG_VERSION")" == "${PG_VERSION%%.*}" ]] || die "$DATA is not a PostgreSQL ${PG_VERSION%%.*} cluster"

  if "$BIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1; then
    echo "Local PostgreSQL already running on 127.0.0.1:$PORT ($DATA)"
    return
  fi
  port_in_use && die "port $PORT is used by another process; refusing to start"
  mkdir -p "$RUN" && chmod 700 "$RUN"
  "$BIN/pg_ctl" -D "$DATA" -l "$LOG" -o "$(server_opts)" -w start >/dev/null
  echo "Local PostgreSQL $PG_VERSION running on 127.0.0.1:$PORT (data: $DATA)"
  echo "Credentials: $CREDENTIALS"
}

initialize() {
  port_in_use && die "port $PORT is used by another process; refusing to initialize"
  mkdir -p "$BASE" "$RUN" && chmod 700 "$BASE" "$RUN"

  # Passwords are generated locally and stored only in a gitignored, owner-only file.
  local superuser_password owner_password runtime_password operator_password worker_password pwfile
  superuser_password="$(random_password)"
  owner_password="$(random_password)"
  runtime_password="$(random_password)"
  operator_password="$(random_password)"
  worker_password="$(random_password)"
  pwfile="$(mktemp "$BASE/pwfile.XXXXXX")"
  printf '%s\n' "$superuser_password" >"$pwfile"

  "$BIN/initdb" -D "$STAGING" -U postgres --pwfile="$pwfile" --auth=scram-sha-256 \
    --encoding=UTF8 --locale=C >"$BASE/initdb.log" 2>&1 || { rm -f "$pwfile"; die "initdb failed; see $BASE/initdb.log"; }
  rm -f "$pwfile"

  "$BIN/pg_ctl" -D "$STAGING" -l "$LOG" -o "$(server_opts)" -w start >/dev/null
  if ! PGPASSWORD="$superuser_password" "$BIN/psql" -X -q -h "$RUN" -p "$PORT" -U postgres -d postgres \
    -v ON_ERROR_STOP=1 -v owner_password="$owner_password" -v runtime_password="$runtime_password" \
    -v operator_password="$operator_password" -v worker_password="$worker_password" -f "$ROLES_SQL" \
    || ! PGPASSWORD="$superuser_password" "$BIN/psql" -X -q -h "$RUN" -p "$PORT" -U postgres -d postgres \
    -v ON_ERROR_STOP=1 -v db_name="$DB_NAME" -v db_purpose=development -f "$DATABASE_SQL"; then
    "$BIN/pg_ctl" -D "$STAGING" -m fast -w stop >/dev/null
    die "bootstrap failed; cluster left at $STAGING"
  fi
  "$BIN/pg_ctl" -D "$STAGING" -m fast -w stop >/dev/null

  (
    umask 077
    cat >"$CREDENTIALS" <<EOF
# Local development credentials for the disposable cluster. Never commit.
LOCAL_PG_SUPERUSER_PASSWORD=$superuser_password
DATABASE_URL_OWNER=postgresql://app_owner:$owner_password@127.0.0.1:$PORT/$DB_NAME
DATABASE_URL_RUNTIME=postgresql://app_runtime:$runtime_password@127.0.0.1:$PORT/$DB_NAME
DATABASE_URL_OPERATOR=postgresql://app_operator:$operator_password@127.0.0.1:$PORT/$DB_NAME
DATABASE_URL_WORKER=postgresql://app_worker:$worker_password@127.0.0.1:$PORT/$DB_NAME
EOF
  )
  touch "$STAGING/.local-dev-cluster"
  mv "$STAGING" "$DATA"
  echo "Initialized PostgreSQL $PG_VERSION cluster at $DATA"
}

cmd_down() {
  if [[ ! -d "$DATA" ]]; then
    echo "No local cluster at $DATA; nothing to stop"
    return
  fi
  [[ -f "$MARKER" ]] || die "$DATA is not a cluster created by this script; refusing to stop it"
  require_binaries
  if ! "$BIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1; then
    echo "Local PostgreSQL is not running"
    return
  fi
  "$BIN/pg_ctl" -D "$DATA" -m fast -w stop >/dev/null
  echo "Local PostgreSQL stopped (data kept at $DATA)"
}

# Connection helpers for a running cluster; they read the superuser password
# from the credentials file.
load_running_cluster() {
  [[ -f "$MARKER" ]] || die "no local cluster; run 'make api-db-up' first"
  require_binaries
  "$BIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1 || die "local PostgreSQL is not running; run 'make api-db-up'"
  # shellcheck source=/dev/null
  . "$CREDENTIALS"
}

superuser_psql() {
  local database="$1"
  shift
  PGPASSWORD="$LOCAL_PG_SUPERUSER_PASSWORD" "$BIN/psql" -X -q -At -h "$RUN" -p "$PORT" \
    -U postgres -d "$database" -v ON_ERROR_STOP=1 "$@"
}

# Clusters created before the operator role existed get it here. Creates
# app_operator if missing (or resets its password if its URL is not in the
# credentials file), verifies its attributes, and grants it CONNECT, USAGE and
# its search_path on the development database. Changes nothing else.
ensure_operator_role() {
  local exists password
  exists="$(superuser_psql postgres -c "SELECT 1 FROM pg_roles WHERE rolname = 'app_operator'")"
  if [[ -z "$exists" || -z "${DATABASE_URL_OPERATOR:-}" ]]; then
    password="$(random_password)"
    if [[ -z "$exists" ]]; then
      superuser_psql postgres -v operator_password="$password" -f - <<'SQL'
CREATE ROLE app_operator LOGIN PASSWORD :'operator_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SQL
    else
      superuser_psql postgres -v operator_password="$password" -f - <<'SQL'
ALTER ROLE app_operator PASSWORD :'operator_password';
SQL
    fi
    (
      umask 077
      grep -v '^DATABASE_URL_OPERATOR=' "$CREDENTIALS" >"$CREDENTIALS.new" || true
      printf 'DATABASE_URL_OPERATOR=postgresql://app_operator:%s@127.0.0.1:%s/%s\n' \
        "$password" "$PORT" "$DB_NAME" >>"$CREDENTIALS.new"
      mv "$CREDENTIALS.new" "$CREDENTIALS"
    )
    DATABASE_URL_OPERATOR="postgresql://app_operator:$password@127.0.0.1:$PORT/$DB_NAME"
    echo "app_operator ready; its URL is in $CREDENTIALS"
  fi
  [[ "$(superuser_psql postgres -c "SELECT rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole \
    OR rolreplication FROM pg_roles WHERE rolname = 'app_operator'")" == "f" ]] \
    || die "app_operator has elevated attributes; refusing to continue"
  superuser_psql "$DB_NAME" -c "GRANT CONNECT ON DATABASE $DB_NAME TO app_operator" \
    -c "GRANT USAGE ON SCHEMA app TO app_operator" \
    -c "ALTER ROLE app_operator IN DATABASE $DB_NAME SET search_path = app"
}

cmd_operator_role() {
  local LOCAL_PG_SUPERUSER_PASSWORD DATABASE_URL_OWNER DATABASE_URL_RUNTIME DATABASE_URL_OPERATOR
  load_running_cluster
  ensure_operator_role
}

# Clusters created before the processing worker existed get app_worker here.
# Creates it if missing (or resets its password if its URL is not in the
# credentials file) and verifies its attributes. Unlike ensure_operator_role it
# grants nothing on the development database: that comes with migration 0008,
# applied there only with separate approval. The disposable test database
# gets its grants from database.sql.
ensure_worker_role() {
  local exists password
  exists="$(superuser_psql postgres -c "SELECT 1 FROM pg_roles WHERE rolname = 'app_worker'")"
  if [[ -z "$exists" || -z "${DATABASE_URL_WORKER:-}" ]]; then
    password="$(random_password)"
    if [[ -z "$exists" ]]; then
      superuser_psql postgres -v worker_password="$password" -f - <<'SQL'
CREATE ROLE app_worker LOGIN PASSWORD :'worker_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SQL
    else
      superuser_psql postgres -v worker_password="$password" -f - <<'SQL'
ALTER ROLE app_worker PASSWORD :'worker_password';
SQL
    fi
    (
      umask 077
      grep -v '^DATABASE_URL_WORKER=' "$CREDENTIALS" >"$CREDENTIALS.new" || true
      printf 'DATABASE_URL_WORKER=postgresql://app_worker:%s@127.0.0.1:%s/%s\n' \
        "$password" "$PORT" "$DB_NAME" >>"$CREDENTIALS.new"
      mv "$CREDENTIALS.new" "$CREDENTIALS"
    )
    DATABASE_URL_WORKER="postgresql://app_worker:$password@127.0.0.1:$PORT/$DB_NAME"
    echo "app_worker ready; its URL is in $CREDENTIALS"
  fi
  [[ "$(superuser_psql postgres -c "SELECT rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole \
    OR rolreplication FROM pg_roles WHERE rolname = 'app_worker'")" == "f" ]] \
    || die "app_worker has elevated attributes; refusing to continue"
}

cmd_worker_role() {
  local LOCAL_PG_SUPERUSER_PASSWORD DATABASE_URL_OWNER DATABASE_URL_RUNTIME DATABASE_URL_OPERATOR DATABASE_URL_WORKER
  load_running_cluster
  ensure_worker_role
}

# Disposable databases (app_test, app_demo) carry their purpose as the database
# comment. They are dropped only when that comment is exactly their marker.

# Prints nothing if the database does not exist, otherwise 'marker=<comment>'.
database_state() {
  superuser_psql postgres -c "SELECT 'marker=' || coalesce(shobj_description(oid, 'pg_database'), '') \
    FROM pg_database WHERE datname = '$1'"
}

# Drop the database if it exists; refuse (and change nothing) unless it carries the marker.
drop_marked_database() {
  local name="$1" marker="$2" state
  [[ "$name" != "$DB_NAME" ]] || die "refusing to drop the development database $DB_NAME"
  state="$(database_state "$name")"
  if [[ -n "$state" ]]; then
    [[ "$state" == "marker=$marker" ]] \
      || die "database $name exists without the '$marker' marker; refusing to drop it"
    superuser_psql postgres -c "DROP DATABASE $name WITH (FORCE)"
  fi
}

create_marked_database() {
  superuser_psql postgres -v db_name="$1" -v db_purpose="$2" -f "$DATABASE_SQL" >/dev/null
}

# Drop and recreate app_test from database.sql, then write the test URLs to
# test.env. An existing app_test is dropped only if it carries the marker.
cmd_test_db() {
  local LOCAL_PG_SUPERUSER_PASSWORD DATABASE_URL_OWNER DATABASE_URL_RUNTIME DATABASE_URL_OPERATOR DATABASE_URL_WORKER
  load_running_cluster
  ensure_operator_role
  ensure_worker_role

  drop_marked_database "$TEST_DB_NAME" "$TEST_DB_MARKER"
  create_marked_database "$TEST_DB_NAME" "$TEST_DB_MARKER"

  (
    umask 077
    printf '%s\n' \
      "# Disposable test database $TEST_DB_NAME, recreated by 'make api-check'. Never commit." \
      "TEST_DATABASE_URL_OWNER=${DATABASE_URL_OWNER%/"$DB_NAME"}/$TEST_DB_NAME" \
      "TEST_DATABASE_URL_RUNTIME=${DATABASE_URL_RUNTIME%/"$DB_NAME"}/$TEST_DB_NAME" \
      "TEST_DATABASE_URL_OPERATOR=${DATABASE_URL_OPERATOR%/"$DB_NAME"}/$TEST_DB_NAME" \
      "TEST_DATABASE_URL_WORKER=${DATABASE_URL_WORKER%/"$DB_NAME"}/$TEST_DB_NAME" \
      "TEST_DATABASE_URL_ADMIN=postgresql://postgres:$LOCAL_PG_SUPERUSER_PASSWORD@127.0.0.1:$PORT/$TEST_DB_NAME" \
      >"$TEST_ENV"
  )
  echo "Recreated disposable test database $TEST_DB_NAME on 127.0.0.1:$PORT"
}

# The disposable demo database app_demo, for local demos and live acceptance with
# synthetic documents only. demo.env holds its URLs under the names the API,
# worker and operator CLI read, so the Makefile's demo targets can load it after
# any other settings. No superuser URL is written.
write_demo_env() {
  (
    umask 077
    printf '%s\n' \
      "# Disposable demo database $DEMO_DB_NAME (scripts/local-postgres.sh demo-db). Never commit." \
      "DATABASE_URL_OWNER=${DATABASE_URL_OWNER%/"$DB_NAME"}/$DEMO_DB_NAME" \
      "DATABASE_URL=${DATABASE_URL_RUNTIME%/"$DB_NAME"}/$DEMO_DB_NAME" \
      "DATABASE_URL_OPERATOR=${DATABASE_URL_OPERATOR%/"$DB_NAME"}/$DEMO_DB_NAME" \
      "DATABASE_URL_WORKER=${DATABASE_URL_WORKER%/"$DB_NAME"}/$DEMO_DB_NAME" \
      >"$DEMO_ENV"
  )
}

cmd_demo_db() {
  local LOCAL_PG_SUPERUSER_PASSWORD DATABASE_URL_OWNER DATABASE_URL_RUNTIME DATABASE_URL_OPERATOR DATABASE_URL_WORKER
  load_running_cluster
  ensure_operator_role
  ensure_worker_role

  local state
  state="$(database_state "$DEMO_DB_NAME")"
  if [[ -z "$state" ]]; then
    create_marked_database "$DEMO_DB_NAME" "$DEMO_DB_MARKER"
    echo "Created disposable demo database $DEMO_DB_NAME on 127.0.0.1:$PORT"
  else
    [[ "$state" == "marker=$DEMO_DB_MARKER" ]] \
      || die "database $DEMO_DB_NAME exists without the '$DEMO_DB_MARKER' marker; refusing to use it"
    echo "Disposable demo database $DEMO_DB_NAME already exists; kept"
  fi
  write_demo_env
}

cmd_demo_reset() {
  local LOCAL_PG_SUPERUSER_PASSWORD DATABASE_URL_OWNER DATABASE_URL_RUNTIME DATABASE_URL_OPERATOR DATABASE_URL_WORKER
  load_running_cluster
  ensure_operator_role
  ensure_worker_role

  drop_marked_database "$DEMO_DB_NAME" "$DEMO_DB_MARKER"
  create_marked_database "$DEMO_DB_NAME" "$DEMO_DB_MARKER"
  write_demo_env
  echo "Recreated disposable demo database $DEMO_DB_NAME on 127.0.0.1:$PORT (all demo data removed)"
}

cmd_demo_drop() {
  local LOCAL_PG_SUPERUSER_PASSWORD DATABASE_URL_OWNER DATABASE_URL_RUNTIME DATABASE_URL_OPERATOR DATABASE_URL_WORKER
  load_running_cluster
  drop_marked_database "$DEMO_DB_NAME" "$DEMO_DB_MARKER"
  rm -f "$DEMO_ENV"
  echo "Dropped disposable demo database $DEMO_DB_NAME (if it existed) and removed $DEMO_ENV"
}

# Run before anything connects with demo.env: the database must exist with its
# marker, and every URL in demo.env must name app_demo on this cluster.
cmd_demo_check() {
  local LOCAL_PG_SUPERUSER_PASSWORD DATABASE_URL_OWNER DATABASE_URL_RUNTIME DATABASE_URL_OPERATOR DATABASE_URL_WORKER
  load_running_cluster
  [[ "$(database_state "$DEMO_DB_NAME")" == "marker=$DEMO_DB_MARKER" ]] \
    || die "no $DEMO_DB_NAME database with the '$DEMO_DB_MARKER' marker; run 'make demo-db'"
  [[ -f "$DEMO_ENV" ]] || die "$DEMO_ENV is missing; run 'make demo-db'"
  local line count=0
  while IFS= read -r line; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" =~ ^DATABASE_URL(_OWNER|_OPERATOR|_WORKER)?=postgresql://[a-z_]+:[^@]+@127\.0\.0\.1:$PORT/$DEMO_DB_NAME$ ]] \
      || die "$DEMO_ENV has an entry that is not an $DEMO_DB_NAME URL on 127.0.0.1:$PORT; run 'make demo-reset'"
    count=$((count + 1))
  done <"$DEMO_ENV"
  ((count == 4)) || die "$DEMO_ENV must hold exactly the four $DEMO_DB_NAME URLs; run 'make demo-db'"
}

case "${1:-}" in
  install) cmd_install ;;
  up) cmd_up ;;
  down) cmd_down ;;
  test-db) cmd_test_db ;;
  operator-role) cmd_operator_role ;;
  worker-role) cmd_worker_role ;;
  demo-db) cmd_demo_db ;;
  demo-reset) cmd_demo_reset ;;
  demo-drop) cmd_demo_drop ;;
  demo-check) cmd_demo_check ;;
  *) die "usage: $0 {install|up|down|test-db|operator-role|worker-role|demo-db|demo-reset|demo-drop|demo-check}" ;;
esac
