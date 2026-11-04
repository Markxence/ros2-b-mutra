#!/usr/bin/env bash
# Smoke test: 4 robots attest successfully, verify each other, then a tampered
# robot is detected. Usage: ./smoke.sh lv|rr|v2|v3 [--remote]   (starts the stack, checks, stops it)
# --remote: run from the core; the robots and their sidecars are on REMOTE1_HOST
# (set in the environment, as for start.sh), reached over SSH.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
USAGE="usage: ./smoke.sh lv|rr|v2|v3 [--remote]"
CONTRACT="${1:?$USAGE}"
N=4
REMOTE=()
SIDECAR_HOST=localhost
case "${2:-}" in
  "") ;;
  --remote)
    : "${REMOTE1_HOST:?--remote needs REMOTE1_HOST}" "${REMOTE1_USER:?--remote needs REMOTE1_USER}"
    REMOTE=(ssh "$REMOTE1_USER@$REMOTE1_HOST")
    SIDECAR_HOST="$REMOTE1_HOST" ;;
  *) echo "$USAGE" >&2; exit 1 ;;
esac

# Sidecar containers run on the robot host; the SECaaS and its database stay local.
sidecar_logs() { "${REMOTE[@]}" docker logs "$1" 2>&1; }
count() { sidecar_logs "$1" | grep -c "Prover.*Result: .*$2" || true; }
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
./start.sh --robots $N --contract "$CONTRACT" --ssp 10000 --iterq 1 ${REMOTE:+--remote} >/dev/null
# V2 and V3 anchor the references on chain: the SECaaS must not need its
# database to verify, so it is made wrong before any cycle runs.
if [[ "$CONTRACT" == v[23] ]]; then
  docker exec attestation-db psql -U postgres -d attestation -qc "UPDATE measures SET combined_hash = repeat('0', 64)"
  echo "ok: SECaaS database made wrong on purpose"
fi
curl -fsS -X POST "http://localhost:8000/start" >/dev/null
for i in $(seq 1 $N); do curl -fsS -X POST "http://$SIDECAR_HOST:$((8000 + i))/start" >/dev/null; done

wait_for 120 all_succeeded || { echo "FAIL: not every robot attested successfully within 120 s"; exit 1; }
no_failure 1 "failed attestation before any tampering"
echo "ok: $N robots attest successfully"

# RR starts in SECaaS-only mode: leave it, so that robots verify each other.
[[ "$CONTRACT" != "rr" ]] || curl -fsS -X POST "http://localhost:8000/secaas-only?enabled=false" >/dev/null
SECAAS_ADDR="$(jq -er .eth_address config/secaas.json)"
peer_verified() {
  for i in $(seq 1 $N); do
    (( $(sidecar_logs "robot$i-sidecar" | grep "Verified by:" | grep -civ "$SECAAS_ADDR" || true) >= 1 )) || return 1
  done
}
jury_verified() {  # V3: every robot had a cycle closed by the jury and voted in one
  for i in $(seq 1 $N); do
    (( $(sidecar_logs "robot$i-sidecar" | grep -c "Prover.*Verified by: the jury" || true) >= 1 )) || return 1
    (( $(sidecar_logs "robot$i-sidecar" | grep -c "Juror.*Vote sent" || true) >= 1 )) || return 1
  done
}
if [[ "$CONTRACT" == v3 ]]; then
  wait_for 120 jury_verified || { echo "FAIL: not every robot was verified by the jury and voted within 120 s"; exit 1; }
  no_failure 1 "failed attestation under jury verification"
  echo "ok: every robot was verified by a jury of peers and voted in one"
else
  wait_for 120 peer_verified || { echo "FAIL: not every robot was verified by a peer within 120 s"; exit 1; }
  no_failure 1 "failed attestation under peer verification"
  echo "ok: every robot was verified by a peer"
fi

if [[ "$CONTRACT" == v[23] ]]; then
  (( $(docker logs secaas 2>&1 | grep -c "ref. measurement sent" || true) == 0 )) \
    || { echo "FAIL: the SECaaS still supplied a reference"; exit 1; }
  echo "ok: the SECaaS supplied no reference"
  resolved=$(docker logs secaas 2>&1 | grep -c "Resolved atomically (result: .*SUCCESS" || true)
  (( resolved >= 1 )) || { echo "FAIL: the SECaaS verified no cycle"; exit 1; }
  echo "ok: the SECaaS verified $resolved cycles against the anchored reference, despite its wrong database"
fi

if [[ ${#REMOTE[@]} -gt 0 ]]; then
  "${REMOTE[@]}" "cd ~/ros2-b-mutra && ./tools/tamper.sh robot1" >/dev/null
else
  ./tools/tamper.sh robot1 >/dev/null
fi
wait_for 60 robot1_failed || { echo "FAIL: tampering of robot1 not detected within 60 s"; exit 1; }
no_failure 2 "reported a failure although only robot1 was tampered"
if [[ "$CONTRACT" == v3 ]]; then
  votes_against=0
  for i in $(seq 2 $N); do
    votes_against=$(( votes_against + $(sidecar_logs "robot$i-sidecar" | grep -c "Juror.*Vote sent (verdict: .*FAILURE" || true) ))
  done
  (( votes_against >= 2 )) || { echo "FAIL: robot1 was not voted down by its jury ($votes_against votes against)"; exit 1; }
  echo "ok: the jury voted robot1 down ($votes_against votes against)"
fi
echo "ok: tampering of robot1 detected, other robots unaffected"
echo "PASS"
