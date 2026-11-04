#!/usr/bin/env bash
# End-to-end expiry test (V3): two jurors go down, the prover freezes right
# after sending its evidence, and the SECaaS must expire the cycle and decide
# it in less than twice the jury timeout. Starts the stack, checks, stops it.
# Usage: ./tools/expiry_test.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
N=4
TIMEOUT_S=10  # 5 blocks of 2 s, the --jury default of start.sh

count() { docker logs "$1" 2>&1 | grep -c "$2" || true; }
wait_for() {  # wait_for <seconds> <command...>
  local deadline=$((SECONDS + $1)); shift
  until "$@"; do (( SECONDS < deadline )) || return 1; sleep 1; done
}
all_succeeded() { for i in $(seq 1 $N); do (( $(count "robot$i-sidecar" "Prover.*Result: .*SUCCESS") >= 1 )) || return 1; done; }

trap "docker unpause robot1-sidecar >/dev/null 2>&1; ./stop.sh >/dev/null 2>&1" EXIT
./start.sh --robots $N --contract v3 --jury 3 --quorum 2 --ssp 10000 --iterq 1 --auto >/dev/null
wait_for 120 all_succeeded || { echo "FAIL: not every robot attested successfully within 120 s"; exit 1; }
echo "ok: $N robots attest successfully"

# robot1's next jury is robot2, robot3 and robot4: with two of them down,
# one vote cannot reach the threshold of 2.
docker stop robot2-sidecar robot3-sidecar >/dev/null
before=$(count robot1-sidecar "Prover.*Waiting for verification result")
new_cycle() { (( $(count robot1-sidecar "Prover.*Waiting for verification result") > before )); }
wait_for 60 new_cycle || { echo "FAIL: robot1 sent no evidence within 60 s"; exit 1; }
docker pause robot1-sidecar >/dev/null
paused=$SECONDS
# The prover log shows the id as att...<tail>, the full id ends with that tail.
tail=$(docker logs robot1-sidecar 2>&1 | grep "Prover.*Waiting for verification result" | tail -1 | grep -o 'Prover-att\.\.\.[^]]*' | cut -c14-)
id="attestation[0-9a-f]*$tail"
echo "ok: robot2 and robot3 down, robot1 frozen after sending cycle ...$tail"

expired() { (( $(count secaas "cycle $id expired by the SECaaS") >= 1 )); }
# The SECaaS worker handles one cycle at a time: the first result after it picks the cycle up is its own.
decided() { docker logs secaas 2>&1 | awk -v re="Processing attestation '$id'" '$0 ~ re {p=1} p && /Resolved atomically/ {f=1} END {exit !f}'; }
wait_for $((2 * TIMEOUT_S)) expired || { echo "FAIL: the SECaaS did not expire cycle ...$tail within $((2 * TIMEOUT_S)) s"; exit 1; }
wait_for $((2 * TIMEOUT_S - (SECONDS - paused))) decided \
  || { echo "FAIL: the SECaaS did not decide cycle ...$tail within $((2 * TIMEOUT_S)) s"; exit 1; }
echo "ok: the SECaaS expired and decided cycle ...$tail in $((SECONDS - paused)) s (limit $((2 * TIMEOUT_S)) s)"
echo "PASS"
