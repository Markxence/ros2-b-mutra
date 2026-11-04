#!/usr/bin/env bash
# Smoke test: 4 robots attest successfully, then a tampered robot is detected.
# Usage: ./smoke.sh lv|rr      (starts the stack, checks, stops it)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
CONTRACT="${1:?usage: ./smoke.sh lv|rr}"
N=4

count() { docker logs "$1" 2>&1 | grep -c "Prover.*Result: .*$2" || true; }
wait_for() {  # wait_for <seconds> <command...>
  local deadline=$((SECONDS + $1)); shift
  until "$@"; do (( SECONDS < deadline )) || return 1; sleep 2; done
}
all_succeeded() { for i in $(seq 1 $N); do (( $(count "robot$i-sidecar" SUCCESS) >= 1 )) || return 1; done; }
robot1_failed() { (( $(count robot1-sidecar FAILURE) >= 1 )); }
no_failure() {  # no_failure <first robot> <message suffix>
  for i in $(seq "$1" $N); do
    (( $(count "robot$i-sidecar" FAILURE) == 0 )) || { echo "FAIL: robot$i $2"; exit 1; }
  done
}

trap "./stop.sh >/dev/null 2>&1" EXIT
./start.sh --robots $N --contract "$CONTRACT" --ssp 10000 --iterq 1 --auto >/dev/null

wait_for 120 all_succeeded || { echo "FAIL: not every robot attested successfully within 120 s"; exit 1; }
no_failure 1 "failed attestation before any tampering"
echo "ok: $N robots attest successfully"

./tools/tamper.sh robot1 >/dev/null
wait_for 60 robot1_failed || { echo "FAIL: tampering of robot1 not detected within 60 s"; exit 1; }
no_failure 2 "reported a failure although only robot1 was tampered"
echo "ok: tampering of robot1 detected, other robots unaffected"
echo "PASS"
