#!/bin/sh
# One-shot stack bootstrap, run by the compose `bootstrap` service before the API
# and the worker start.
#
# Why this exists: the runbook used to ask the reader to apply the migrations and
# create the first accounts by hand, in the right order, after the stack was up.
# That is three commands to *forget* - and a schema that is one revision behind
# fails in ways that look like application bugs. Running them from a container
# that the API and the worker `depends_on: service_completed_successfully` gives
# the same explicitness (a migration is applied once, in one place, visibly in
# the logs) with a single `docker compose up`.
#
# Everything here is idempotent, because this container runs on every `up`, not
# just the first one.
set -eu

log() { echo "[bootstrap] $*"; }

# ------------------------------------------------------------------ migrations
# `upgrade head` is idempotent by construction: with the schema at head it is a
# no-op. A *failing* migration exits non-zero here, which is what stops the API
# and the worker from starting against a half-migrated database.
log "applying database migrations"
alembic upgrade head
log "schema at $(alembic current 2>/dev/null | tail -n 1)"

# ------------------------------------------------------------- object storage
# Garage's own bootstrap already created the buckets (that is what its
# healthcheck waits for). Reapplying is cheap and does two useful things: it
# converges a stack pointed at an *external* S3, and it (re)applies the bucket
# CORS rules the browser needs to fetch presigned objects - a presigned URL
# authorises the request, not the JavaScript.
log "ensuring the object-storage buckets and their CORS rules"
python -m app.cli init-storage

# -------------------------------------------------------------- seed accounts
# Bootstrapping the first Admin cannot go through the API: /users already needs
# an Admin, and the first one does not exist yet. The CLI is that out-of-band
# path; `--if-exists skip` keeps a second `up` from failing here.
seed_account() {
  role="$1"
  email="$2"
  password="$3"

  if [ -z "$email" ]; then
    log "not seeding a $role: the email is empty"
    return 0
  fi
  if [ -z "$password" ]; then
    # Abort rather than continue: an Admin that silently does not exist is a
    # worse failure than a stack that refuses to come up.
    echo "[bootstrap] ERROR: BOOTSTRAP_$(echo "$role" | tr '[:lower:]' '[:upper:]')_PASSWORD is empty for <$email>" >&2
    exit 1
  fi

  python -m app.cli create-user --email "$email" --password "$password" --role "$role" --if-exists skip
}

seed_account admin "${BOOTSTRAP_ADMIN_EMAIL:-}" "${BOOTSTRAP_ADMIN_PASSWORD:-}"
seed_account inspector "${BOOTSTRAP_INSPECTOR_EMAIL:-}" "${BOOTSTRAP_INSPECTOR_PASSWORD:-}"

log "done - the schema is at head and the seed accounts exist"
