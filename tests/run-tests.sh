#!/usr/bin/env bash
# Run all B-MuTRA test suites: rolling-hash vectors (Go + Python) and
# the AttestationManagerLV Hardhat tests. Each suite is run in
# its own throwaway container so no host-side toolchain is required.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }

fail=0
run_suite() {
  local name="$1"; shift
  bold "=== $name ==="
  if "$@"; then
    green "  PASS — $name"
  else
    red "  FAIL — $name"
    fail=1
  fi
  echo
}

# ---------------------------------------------------------------------------
# Suite 1 — Python rolling_hash
# ---------------------------------------------------------------------------
run_suite "Python rolling_hash" \
  docker run --rm \
    -v "$REPO_DIR":/repo -w /repo \
    python:3.11-slim \
    bash -c 'pip install --quiet pytest && pytest dockerfiles/secaas/tests/ -q'

# ---------------------------------------------------------------------------
# Suite 2 — Go RollingHash
# ---------------------------------------------------------------------------
run_suite "Go RollingHash" \
  docker run --rm \
    -v "$REPO_DIR":/repo \
    -w /repo/dockerfiles/attestation-sidecar/app \
    golang:1.25-bookworm \
    go test ./internal/compute/ -count=1

# ---------------------------------------------------------------------------
# Suite 3 — Hardhat smart-contract tests
# ---------------------------------------------------------------------------
# Requires the project's hardhat:latest image (built by `cd dockerfiles && make hardhat`).
run_suite "Hardhat contracts (LV, V2)" \
  docker run --rm \
    -v "$REPO_DIR/smart-contracts/contracts":/smart-contracts/contracts:ro \
    -v "$REPO_DIR/smart-contracts/test":/smart-contracts/test:ro \
    -v "$REPO_DIR/smart-contracts/scripts":/smart-contracts/scripts:ro \
    -v "$REPO_DIR/smart-contracts/hardhat.config.js":/smart-contracts/hardhat.config.js:ro \
    hardhat:latest \
    bash -c "cd /smart-contracts && npx hardhat test"

if (( fail )); then
  red "Some suites failed."
  exit 1
fi
green "All test suites passed."
