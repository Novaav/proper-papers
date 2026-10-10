API_DIR := apps/api
UV := uv --directory $(API_DIR)

# The isolated PDF parser (DI-1): its own locked environment and container image.
PARSER_DIR := apps/parser
PARSER_UV := uv --directory $(PARSER_DIR)
PARSER_IMAGE ?= production-ai-flagship-parser:local
DOCKER ?= docker

WEB_DIR := apps/web
# pnpm runs at the version pinned in apps/web/package.json via corepack (bundled with Node 24).
# Corepack reads the pin from the working directory, so it must run inside $(WEB_DIR).
PNPM := cd $(WEB_DIR) && COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack pnpm

.PHONY: check parser-setup parser-format parser-check parser-image parser-measure eval eval-baseline eval-assisted api-setup api-format api-check api-run api-worker api-scanner-up api-scanner-status api-scanner-update api-scanner-down api-live-scan-check api-admin api-db-operator-role api-db-install api-db-up api-db-down api-db-migrate api-test-db api-test-db-migrate web-node web-setup web-format web-check web-dev web-dev-https demo-db demo-reset demo-drop demo-api demo-worker demo-admin demo-corpus

check: parser-check api-check web-check

api-setup:
	$(UV) sync --locked

api-format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

# Development database URLs (DATABASE_URL_OWNER/_RUNTIME) come from the local cluster's
# credentials file. Tests use only the disposable test database app_test
# (TEST_DATABASE_URL_OWNER/_RUNTIME): locally from test.env, written by `api-test-db`;
# in CI exported by the workflow. Without them, the checks fail.
LOCAL_DB_ENV := .local/postgres/credentials.env
# Optional, gitignored settings for `make api-run` and `make api-worker` (AUTH_*,
# CLAMD_ADDRESS=tcp://127.0.0.1:3310, DATABASE_URL, PARSER_IMAGE_ID, PROCESSING_*, ...).
LOCAL_API_ENV := .local/api.env
LOCAL_TEST_DB_ENV := .local/postgres/test.env
WITH_DB_ENV := if [ -f $(LOCAL_DB_ENV) ]; then set -a; . ./$(LOCAL_DB_ENV); set +a; fi;
WITH_TEST_DB_ENV := if [ -f $(LOCAL_TEST_DB_ENV) ]; then set -a; . ./$(LOCAL_TEST_DB_ENV); set +a; fi;
WITH_API_ENV := if [ -f $(LOCAL_API_ENV) ]; then set -a; . ./$(LOCAL_API_ENV); set +a; fi;

# The only way migrations are applied to the development database; the API never migrates on startup.
api-db-migrate:
	@$(WITH_DB_ENV) $(UV) run --locked alembic upgrade head

# Drop and recreate the local disposable test database (only if it carries the disposable marker).
api-test-db:
	scripts/local-postgres.sh test-db

api-test-db-migrate:
	@$(WITH_TEST_DB_ENV) DATABASE_URL_OWNER="$$TEST_DATABASE_URL_OWNER" $(UV) run --locked alembic upgrade head

# Locally, recreates the test database first; in CI the workflow provisions it.
# `alembic check` fails if the models and migrations differ or the database is not at head.
api-check:
	$(UV) run --locked ruff format --check .
	$(UV) run --locked ruff check .
	$(UV) run --locked mypy
	@if [ -f $(LOCAL_DB_ENV) ]; then scripts/local-postgres.sh test-db; fi
	@$(WITH_TEST_DB_ENV) DATABASE_URL_OWNER="$$TEST_DATABASE_URL_OWNER" $(UV) run --locked alembic upgrade head
	@$(WITH_TEST_DB_ENV) DATABASE_URL_OWNER="$$TEST_DATABASE_URL_OWNER" $(UV) run --locked alembic check
	@$(WITH_TEST_DB_ENV) $(UV) run --locked pytest

# Parser package (host run of the parsing code; the real isolation is tested
# through the image by the API's sandbox tests: PARSER_IMAGE=... make api-check).
parser-setup:
	$(PARSER_UV) sync --locked

parser-format:
	$(PARSER_UV) run --locked ruff format --config pyproject.toml . ../../corpus
	$(PARSER_UV) run --locked ruff check --config pyproject.toml --fix . ../../corpus

# The corpus must regenerate to exactly the committed manifest, and the image's
# hash-locked requirements must match uv.lock.
parser-check:
	$(PARSER_UV) run --locked ruff format --config pyproject.toml --check . ../../corpus
	$(PARSER_UV) run --locked ruff check --config pyproject.toml . ../../corpus
	$(PARSER_UV) run --locked mypy
	$(PARSER_UV) run --locked pytest
	python3 corpus/generate.py check
	$(PARSER_UV) export --locked --no-dev --no-emit-project --format requirements-txt \
		--quiet -o requirements.txt
	git diff --exit-code -- $(PARSER_DIR)/requirements.txt

parser-image:
	$(DOCKER) build --tag $(PARSER_IMAGE) $(PARSER_DIR)

# Limits measurement through the real sandbox (report only).
parser-measure:
	PARSER_IMAGE=$(PARSER_IMAGE) PARSER_DOCKER=$(DOCKER) $(UV) run --locked python -m scripts.measure_parser

# DI-1 extraction evaluation through the real parser sandbox (scripts/evaluate.py):
# dev gates are hard; each held-out split (heldout, heldout-2) must not regress against
# its corpus/eval/<split>-baseline.json.
eval:
	PARSER_IMAGE=$(PARSER_IMAGE) PARSER_DOCKER=$(DOCKER) $(UV) run --locked python -m scripts.evaluate

# Rewrites one held-out split's committed baseline (SPLIT=heldout, heldout-2 or heldout-3; only if every
# hard gate passes); review the diff.
eval-baseline:
	@test -n "$(SPLIT)" || { echo "eval-baseline: set SPLIT=heldout, SPLIT=heldout-2 or SPLIT=heldout-3"; exit 2; }
	PARSER_IMAGE=$(PARSER_IMAGE) PARSER_DOCKER=$(DOCKER) $(UV) run --locked python -m scripts.evaluate --write-baseline $(SPLIT)

# AI1 model-assisted extraction evaluation (scripts/eval_assisted.py). Without ARGS it replays
# every frozen profile's committed responses (offline, no key, no network): hard gates per run,
# the pre-registered decision, and equality with the committed report. Live calls are paid and
# need explicit ARGS (--profile P --live --run N --spend-cap USD), the gitignored key file and,
# for held-out cases, the profile's frozen configuration.
eval-assisted:
	PARSER_IMAGE=$(PARSER_IMAGE) PARSER_DOCKER=$(DOCKER) $(UV) run --locked python -m scripts.eval_assisted $(ARGS)

api-run:
	@$(WITH_API_ENV) API_DOCS=1 $(UV) run --locked uvicorn api.main:app --host 127.0.0.1 --port 8000

# The DI-1 processing worker (python -m api.worker; pass ARGS=--once for one run). It needs
# DATABASE_URL_WORKER (app_worker; the local cluster's credentials) and PARSER_IMAGE_ID (the
# full id of the parser image, e.g. in .local/api.env) and refuses to start without them.
api-worker:
	@$(WITH_DB_ENV) $(WITH_API_ENV) $(UV) run --locked python -m api.worker $(ARGS)

# Local malware scanner (the CI-pinned ClamAV image via Docker, 127.0.0.1:3310 only;
# see scripts/local-clamav.sh). Without Docker these stop with instructions and change nothing.
api-scanner-up:
	scripts/local-clamav.sh up

api-scanner-status:
	scripts/local-clamav.sh status

api-scanner-update:
	scripts/local-clamav.sh update

api-scanner-down:
	scripts/local-clamav.sh down

# End-to-end scan checks against a running API (see apps/api/scripts/live_scan_check.py).
api-live-scan-check:
	@$(UV) run --locked python -m scripts.live_scan_check

# Operator CLI (tenant and membership administration) against the development database,
# as app_operator (DATABASE_URL_OPERATOR). Example:
#   make api-admin ARGS='tenant show --tenant-id <uuid>'
api-admin:
	@$(WITH_DB_ENV) $(UV) run --locked python -m api.operator $(ARGS)

# Disposable local PostgreSQL 18 on 127.0.0.1:55432, data in .local/postgres (see scripts/local-postgres.sh).
api-db-install:
	scripts/local-postgres.sh install

api-db-up:
	scripts/local-postgres.sh up

# Adds the app_operator role to a cluster created before it existed (idempotent).
api-db-operator-role:
	scripts/local-postgres.sh operator-role

api-db-down:
	scripts/local-postgres.sh down

# Fail fast when the active Node major differs from apps/web/.nvmrc.
WEB_NODE_VERSION_FILE := $(WEB_DIR)/.nvmrc

web-node:
	@node $(WEB_DIR)/scripts/check-node-version.mjs $(WEB_NODE_VERSION_FILE)

web-setup: web-node
	$(PNPM) install --frozen-lockfile

web-format: web-node
	$(PNPM) run format

web-check: web-node
	$(PNPM) run format:check
	$(PNPM) run lint
	$(PNPM) run typecheck
	$(PNPM) run test
	$(PNPM) run build
	$(PNPM) run check:bundle
	$(PNPM) run check:runtime

web-dev: web-node
	$(PNPM) run dev

# Development server on https://localhost:3000 (bound to localhost only) with the same cookie
# flags as production.
# Uses a self-signed certificate made with openssl in .local/web-tls (gitignored); no CA is
# installed and nothing is downloaded (unlike `next dev --experimental-https` without a key).
WEB_TLS_DIR := $(CURDIR)/.local/web-tls
web-dev-https: web-node
	@if [ ! -f "$(WEB_TLS_DIR)/localhost-key.pem" ]; then \
	  mkdir -p "$(WEB_TLS_DIR)" && chmod 700 "$(WEB_TLS_DIR)" && \
	  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=localhost" \
	    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
	    -keyout "$(WEB_TLS_DIR)/localhost-key.pem" -out "$(WEB_TLS_DIR)/localhost.pem" 2>/dev/null && \
	  chmod 600 "$(WEB_TLS_DIR)/localhost-key.pem"; \
	fi
	$(PNPM) exec next dev --experimental-https --hostname localhost \
	  --experimental-https-key "$(WEB_TLS_DIR)/localhost-key.pem" \
	  --experimental-https-cert "$(WEB_TLS_DIR)/localhost.pem"

# Local demo and live acceptance on the disposable demo database app_demo (see docs/demo.md).
# It lives in the same local cluster, carries the database comment 'disposable-demo-database',
# and its URLs are in .local/postgres/demo.env. Every demo target first runs
# `local-postgres.sh demo-check`, which fails unless app_demo exists with its marker and every
# URL in demo.env names it; demo.env is loaded after .local/api.env, so its URLs win.
# The development database app is never used here.
LOCAL_DEMO_DB_ENV := .local/postgres/demo.env
WITH_DEMO_DB_ENV := scripts/local-postgres.sh demo-check && set -a && . ./$(LOCAL_DEMO_DB_ENV) && set +a &&
# The synthetic dev corpus cases the demo uses: labelled (complete), date conflict, missing
# required field, unparseable value.
DEMO_CASES := dev-n1-labelled dev-c1-conflict-date dev-m2-required-missing dev-u1-unparseable
DEMO_CORPUS_DIR ?= .local/demo-corpus

# Create app_demo if missing (an existing one is kept) and migrate it to head.
demo-db:
	scripts/local-postgres.sh demo-db
	@$(WITH_DEMO_DB_ENV) $(UV) run --locked alembic upgrade head

# Drop and recreate app_demo (only if it carries its marker) and migrate it: all demo data is lost.
demo-reset:
	scripts/local-postgres.sh demo-reset
	@$(WITH_DEMO_DB_ENV) $(UV) run --locked alembic upgrade head

# Drop app_demo (only if it carries its marker) and remove demo.env.
demo-drop:
	scripts/local-postgres.sh demo-drop

demo-api:
	@$(WITH_API_ENV) $(WITH_DEMO_DB_ENV) API_DOCS=1 $(UV) run --locked uvicorn api.main:app --host 127.0.0.1 --port 8000

demo-worker:
	@$(WITH_API_ENV) $(WITH_DEMO_DB_ENV) $(UV) run --locked python -m api.worker $(ARGS)

#   make demo-admin ARGS='tenant show --tenant-id <uuid>'
demo-admin:
	@$(WITH_DEMO_DB_ENV) $(UV) run --locked python -m api.operator $(ARGS)

# Write the demo cases' PDFs (checked against corpus/manifest.json first) to DEMO_CORPUS_DIR.
demo-corpus:
	@tmp="$$(mktemp -d)" && trap 'rm -rf "$$tmp"' EXIT && \
	  python3 corpus/generate.py build "$$tmp" && \
	  mkdir -p "$(DEMO_CORPUS_DIR)" && \
	  for c in $(DEMO_CASES); do cp "$$tmp/$$c.pdf" "$(DEMO_CORPUS_DIR)/"; done && \
	  echo "demo-corpus: $(DEMO_CASES) -> $(DEMO_CORPUS_DIR)"
