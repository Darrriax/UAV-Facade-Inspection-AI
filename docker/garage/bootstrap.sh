#!/bin/sh
# Start the Garage daemon and make this node usable as an S3 endpoint.
#
# Garage needs three things before the S3 API will accept a request:
#   1. a cluster layout with this node assigned a role (otherwise it answers
#      "NO ROLE ASSIGNED FOR CLUSTER" and every bucket operation fails)
#   2. an S3 access key
#   3. buckets, with that key allowed to read and write them
#
# Everything here is idempotent, so `docker compose up` works on the first boot
# and on every boot after. Where a step cannot be made idempotent (the layout),
# the outcome is *verified* afterwards instead of being assumed - an early
# version of this script treated every CLI failure as "already done", which
# silently produced a cluster with no key and no permissions.
set -eu

CONFIG="${GARAGE_CONFIG_FILE:-/etc/garage.toml}"
ZONE="${GARAGE_LAYOUT_ZONE:-dc1}"
CAPACITY="${GARAGE_LAYOUT_CAPACITY:-1GB}"

log() { echo "[garage] $*"; }
fail() { echo "[garage] ERROR: $*" >&2; exit 1; }

# `garage` talks to a node over RPC, so the daemon has to run in this same
# container - the same arrangement Garage's quickstart sets up by hand with
# `docker exec garage /garage ...`.
/garage -c "$CONFIG" server &
GARAGE_PID=$!

shutdown() {
  log "shutting down the Garage daemon"
  kill -TERM "$GARAGE_PID" 2>/dev/null || true
  wait "$GARAGE_PID" 2>/dev/null || true
}
trap shutdown TERM INT

log "waiting for the daemon to accept admin operations"
attempt=0
until /garage -c "$CONFIG" status >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -gt 60 ]; then
    fail "the daemon did not become ready within 60s"
  fi
  # Distinguish "still starting" from "died on a bad config" so the real error
  # is not buried behind a timeout.
  if ! kill -0 "$GARAGE_PID" 2>/dev/null; then
    log "the daemon exited during startup - see the messages above"
    wait "$GARAGE_PID" || true
    exit 1
  fi
  sleep 1
done
log "daemon is up"

# `node id -q` prints "<public key>@<ip>:<port>"; the layout wants the key.
NODE_ID="$(/garage -c "$CONFIG" node id -q | head -n 1 | cut -d@ -f1)"
log "node id: ${NODE_ID}"

# Assign + apply are not idempotent (they error on a re-run), so failures are
# tolerated here and the result is verified immediately below.
/garage -c "$CONFIG" layout assign -z "$ZONE" -c "$CAPACITY" "$NODE_ID" >/dev/null 2>&1 ||
  log "layout assign refused (already assigned?) - verifying below"
/garage -c "$CONFIG" layout apply --version 1 >/dev/null 2>&1 ||
  log "layout apply refused (already applied?) - verifying below"

if /garage -c "$CONFIG" status 2>/dev/null | grep -q "$ZONE"; then
  log "cluster layout active (zone ${ZONE}, capacity ${CAPACITY})"
else
  /garage -c "$CONFIG" status >&2 || true
  fail "the node still has no role in the cluster layout; see the status above"
fi

# Import rather than create: `key create` generates random credentials that
# would not match the S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY the API is
# configured with. Garage requires the id to be "GK" + 12 hex-encoded bytes.
if /garage -c "$CONFIG" key info "$ACCESS_KEY_ID" >/dev/null 2>&1; then
  log "S3 access key ${ACCESS_KEY_ID} already imported"
else
  /garage -c "$CONFIG" key import --yes -n bootstrap "$ACCESS_KEY_ID" "$SECRET_ACCESS_KEY" >/dev/null ||
    fail "could not import the S3 access key ${ACCESS_KEY_ID}"
  log "imported S3 access key ${ACCESS_KEY_ID}"
fi

for bucket in "$BUCKET_IMAGES" "$BUCKET_MODELS"; do
  if /garage -c "$CONFIG" bucket info "$bucket" >/dev/null 2>&1; then
    log "bucket ${bucket} already exists"
  else
    /garage -c "$CONFIG" bucket create "$bucket" >/dev/null ||
      fail "could not create bucket ${bucket}"
    log "created bucket ${bucket}"
  fi

  # Idempotent: re-applying the same flags succeeds.
  /garage -c "$CONFIG" bucket allow --read --write --owner "$bucket" \
    --key "$ACCESS_KEY_ID" >/dev/null ||
    fail "could not grant ${ACCESS_KEY_ID} access to ${bucket}"
  log "granted ${ACCESS_KEY_ID} read/write/owner on ${bucket}"
done

log "ready: S3 API on :3900, buckets ${BUCKET_IMAGES} and ${BUCKET_MODELS}"

# Hand PID 1 over to the daemon so signals and its exit code propagate.
wait "$GARAGE_PID"
