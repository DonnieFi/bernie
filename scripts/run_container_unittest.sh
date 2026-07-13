#!/usr/bin/env bash
# Serialize bernie-api unittests so parallel agents/pre-commit don't overload bernie-host.
#
# Usage (from repo root or any cwd):
#   ./scripts/run_container_unittest.sh tests.test_foo tests.test_bar
#   ./scripts/run_container_unittest.sh --timeout 180 tests.test_schema_migrations
#
# Lock: flock on the Docker host (bernie-host). Only one unittest batch runs at a time.
# Prefer this over raw `docker exec … unittest` or full discover under load.
#
# Env:
#   BERNIE_TEST_LOCK     lock path on docker host (default /tmp/bernie-unittest.lock)
#   BERNIE_TEST_LOCK_WAIT seconds to wait for lock (default 600)
#   BERNIE_TEST_HOST      ssh target if not already on bernie-host (default operator@bernie-host)
#   BERNIE_TEST_CONTAINER docker name (default bernie-api)
#   BERNIE_TEST_TIMEOUT   unittest wall clock seconds (default 120)

set -euo pipefail

LOCK="${BERNIE_TEST_LOCK:-/tmp/bernie-unittest.lock}"
LOCK_WAIT="${BERNIE_TEST_LOCK_WAIT:-600}"
HOST="${BERNIE_TEST_HOST:-operator@bernie-host}"
CONTAINER="${BERNIE_TEST_CONTAINER:-bernie-api}"
TIMEOUT="${BERNIE_TEST_TIMEOUT:-120}"

MODULES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --lock-wait) LOCK_WAIT="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) MODULES+=("$1"); shift ;;
  esac
done

if [[ ${#MODULES[@]} -eq 0 ]]; then
  echo "usage: $0 [--timeout N] tests.module [tests.module ...]" >&2
  exit 2
fi

# On bernie-host: flock local. Elsewhere: ssh + flock on remote (lock lives with docker).
_on_suji=0
if [[ "$(hostname -s 2>/dev/null || true)" == "bernie-host" ]] || [[ -d /opt/family-bot && -S /var/run/docker.sock ]]; then
  # Prefer local docker when available (workspace often IS the homelab host)
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
    _on_suji=1
  fi
fi

_run_inner() {
  # shellcheck disable=SC2068
  timeout "${TIMEOUT}s" docker exec -w /app -e BERNIE_TESTING=1 -e ROLE=monolith "$CONTAINER" \
    python -m unittest ${MODULES[@]} -v
}

_label="${MODULES[*]}"
echo "🔒 unittest lock ($LOCK, wait≤${LOCK_WAIT}s) — ${#MODULES[@]} module(s)"
echo "   modules: $_label"
echo "   container=$CONTAINER timeout=${TIMEOUT}s"

if [[ $_on_suji -eq 1 ]]; then
  # Exclusive lock; -w waits so agents queue instead of pile on
  if ! command -v flock >/dev/null 2>&1; then
    echo "⚠️  flock not found — running without lock (install util-linux)" >&2
    _run_inner
    exit $?
  fi
  # shellcheck disable=SC2034
  exec 9>"$LOCK"
  if ! flock -w "$LOCK_WAIT" 9; then
    echo "❌ Could not acquire $LOCK within ${LOCK_WAIT}s — another agent testing?" >&2
    echo "   Check: ps aux | grep unittest; cat $LOCK.holder 2>/dev/null" >&2
    exit 75  # EX_TEMPFAIL
  fi
  echo "$$ $(date -Is) ${_label}" >"${LOCK}.holder" 2>/dev/null || true
  set +e
  _run_inner
  rc=$?
  set -e
  rm -f "${LOCK}.holder" 2>/dev/null || true
  exit "$rc"
fi

# Remote path: hold lock on bernie-host for the whole docker exec
REMOTE_CMD=$(cat <<EOF
set -euo pipefail
exec 9>"$LOCK"
if ! flock -w $LOCK_WAIT 9; then
  echo "❌ Could not acquire $LOCK within ${LOCK_WAIT}s on \$(hostname)" >&2
  exit 75
fi
echo "\$\$ \$(date -Is) ${_label}" >"${LOCK}.holder" 2>/dev/null || true
set +e
timeout ${TIMEOUT}s docker exec -w /app -e BERNIE_TESTING=1 -e ROLE=monolith $CONTAINER \\
  python -m unittest ${MODULES[*]} -v
rc=\$?
rm -f "${LOCK}.holder" 2>/dev/null || true
exit \$rc
EOF
)

ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "bash -s" <<<"$REMOTE_CMD"
