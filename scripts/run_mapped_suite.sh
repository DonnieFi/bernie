#!/usr/bin/env bash
# Phase 40B Wave 0a — mapped unittest gate (40-SPEC.md §7, 40-PLAN.md § Verification).
#
# Run on bernie-host from repo root: ssh operator@bernie-host 'cd /opt/family-bot && ./scripts/run_mapped_suite.sh'
#
# Gate strategy: curated named-module list from tests/gate_manifest.txt (40B-m51).
# Edit the manifest to include new tests (no shell script changes needed).
# Full `discover` is best-effort / nightly only. Known pre-existing debt excluded here:
#   - test_calendar_tools: googleapiclient import-time ordering under discover
#   - test_executor_smol: llm_for mock setUp ordering under full discover
#   - test_caching_integration: same llm_for ordering issue
# These pass fine when invoked as named modules (as pre-commit does).
#
# Host legs need a venv with locked deps (created on first run as .test-venv/).
# Container leg uses bare `docker build` + `docker run` with volume mounts because
# bot/.dockerignore excludes tests/ from production images.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export BERNIE_TEST_CONFIG="${BERNIE_TEST_CONFIG:-$ROOT/config.json}"

VENV="${BERNIE_TEST_VENV:-$ROOT/.test-venv}"
PY="$VENV/bin/python"

ensure_venv() {
    local lock_stamp="$VENV/.requirements.lock.stamp"
    if [[ ! -x "$PY" ]]; then
        echo "Creating $VENV from bot/requirements.lock ..."
        python3 -m venv "$VENV"
        "$VENV/bin/pip" install -q -r bot/requirements.lock
        cp -f bot/requirements.lock "$lock_stamp"
        return
    fi
    if [[ ! -f "$lock_stamp" ]] || [[ bot/requirements.lock -nt "$lock_stamp" ]]; then
        echo "Refreshing $VENV (requirements.lock changed) ..."
        if ! "$VENV/bin/pip" install -q -r bot/requirements.lock; then
            echo "pip refresh failed — recreating $VENV ..."
            rm -rf "$VENV"
            python3 -m venv "$VENV"
            "$VENV/bin/pip" install -q -r bot/requirements.lock
        fi
        cp -f bot/requirements.lock "$lock_stamp"
    fi
}

# ---------------------------------------------------------------------------
# Curated module lists (loaded from manifest for auto-include per 40B-m51)
# ---------------------------------------------------------------------------

# Gate manifest: tests/gate_manifest.txt
# Add new tests to the manifest instead of editing this script.

_load_gate_list() {
    local section="$1"
    local file="tests/gate_manifest.txt"
    local list=()
    local in_section=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        trimmed="${line#"${line%%[![:space:]]*}"}" # ltrim
        trimmed="${trimmed%"${trimmed##*[![:space:]]}"}" # rtrim
        if [[ "$trimmed" == "# === "* ]]; then
            if [[ "$trimmed" == "# === ${section} ==="* ]]; then
                in_section=1
            else
                in_section=0
            fi
            continue
        fi
        line="${line%%#*}"   # strip inline comments
        line="${line#"${line%%[![:space:]]*}"}" # ltrim
        line="${line%"${line##*[![:space:]]}"}" # rtrim
        [[ -z "$line" ]] && continue
        if [[ $in_section -eq 1 ]]; then
            list+=("$line")
        fi
    done < "$file"
    printf '%s\n' "${list[@]}"
}

# Leg 1: host bot/tests — named modules (PYTHONPATH=bot)
mapfile -t BOT_TESTS < <(_load_gate_list "BOT_TESTS")

# Leg 2: host root tests/ — named modules (PYTHONPATH=bot)
mapfile -t ROOT_TESTS < <(_load_gate_list "ROOT_TESTS")

run_host_bot_tests() {
    echo -e "\n=== host: bot/tests (curated named modules) ==="
    PYTHONPATH=bot "$PY" -m unittest -v "${BOT_TESTS[@]}"
}

run_host_root_tests() {
    # 1ov.1: ROOT_TESTS empty after consolidation into bot/tests/
    if [[ ${#ROOT_TESTS[@]} -eq 0 || -z "${ROOT_TESTS[0]:-}" ]]; then
        echo -e "\n=== host: tests/ (ROOT_TESTS empty — skipped) ==="
        return 0
    fi
    echo -e "\n=== host: tests/ (curated named modules) ==="
    cd tests || exit 1
    PYTHONPATH=../bot "$PY" -m unittest -v "${ROOT_TESTS[@]}"
    cd ..
}

run_container_smoke() {
    echo "=== container: docker build + run (smoke set) ==="
    docker build -t bernie-test ./bot
    docker run --rm \
        -v "$ROOT/bot/tests:/app/tests:ro" \
        -v "$ROOT/config.json:/app/config.json:ro" \
        bernie-test \
        python -m unittest \
        tests.test_cognition_write tests.test_db_write_stragglers tests.test_phase13_db \
        tests.test_internal_discord_auth tests.test_tools_import_gate -v
}

ensure_venv
run_host_bot_tests
run_host_root_tests
run_container_smoke
echo "✅ mapped suite complete"
