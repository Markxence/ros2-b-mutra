#!/usr/bin/env bash
# run_formation_campaign.sh — automate the swarm FORMATION mission-impact campaign.
#
# Companion to run_campaign.sh (the patrol experiment). Runs the three scenarios
# that produce the formation "progress / formation-error vs time" figures, each
# repeated REPS times, tearing the stack down fresh between runs:
#
#   baseline            no attack                 -> swarm reaches the goal cleanly
#   unmitigated         attack robot3, MIT=off    -> swarm stalls, never recovers
#   mitigated (sweep)   attack robot3, MIT=on, SSP in {SSP_LIST}
#                       -> stalls then recovers (stall width ~ detection latency SSP+B)
#
# The attack is the steering-integrity fault (tools/tamper.sh ... formation_agent):
# robot3's steering is corrupted into a constant turn, so it spins and — through
# the APF cohesion coupling — drags/anchors the whole formation, degrading the
# swarm's progress toward the goal. D-MUTRA detection -> the orchestrator isolates
# robot3 (/formation/excluded) -> robots 1/2/4 reform the triangle and resume.
#
# Per run the controller:
#   1. ./start.sh ... --formation --run-tag <tag>    (fresh full stack, attests formation_agent)
#   2. wait for the orchestrator "Goal published" (mission begin / bag t0)
#   3. (attack runs) wait ATTACK_DELAY_S of travel, then sync to the next victim
#      attestation boundary ("Final digest") and inject — worst-case anchoring so
#      detection latency = SSP + B for every trial (SSP-proportional stalls).
#   4. wait for events-<tag>.json (orchestrator fixed window elapsed)
#   5. ./stop.sh  (teardown; this FLUSHES the bag), then extract metrics offline
#      from the recorded bag, then cooldown, next run.
#
# Each run yields, under experiments/data/formation/:
#   bags/<tag>/            the recorded ros2 bag (ground-truth + odom + markers)
#   events-<tag>.json      orchestrator run metadata + on-chain failure time
#   formation-<tag>.csv    per-step FE / cumulative-damage / progress / cross-track
#   metrics-<tag>.json     headline summary (progress_pct, cumulative_damage, ...)
#
# Usage (run in small pieces first to inspect):
#   ./tools/run_formation_campaign.sh --scenarios baseline    --reps 1
#   ./tools/run_formation_campaign.sh --scenarios unmitigated --reps 1
#   ./tools/run_formation_campaign.sh --scenarios mitigated --ssp-list 10000 --reps 1
#   # full sweep, 5 reps:
#   ./tools/run_formation_campaign.sh --reps 5 --ssp-list "10000 20000 30000 60000"
#
# There is always exactly ONE victim (robot3) and the swarm size is fixed at 4.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# ── Tunables ──────────────────────────────────────────────────────────────────
REPS="${REPS:-3}"
REP_START="${REP_START:-1}"
VICTIM="${VICTIM:-robot3}"               # the formation slot tables assume robot3 is the victim
SSP_LIST="${SSP_LIST:-10000 20000 30000}"
BASELINE_SSP="${BASELINE_SSP:-10000}"
UNMIT_SSP="${UNMIT_SSP:-10000}"
SCENARIOS="${SCENARIOS:-baseline unmitigated mitigated}"
DATA_SUBDIR="${DATA_SUBDIR:-formation}"      # output folder under experiments/data/; set to
                                             # e.g. "formation-v2" for cohesion-drift runs

# Formation mission geometry. Goal 20 m (≈90 s traverse at burger top speed) keeps
# runs to ~3 min while leaving room for the worst-case mitigated stall to recover
# inside the window. RUN_DURATION_S is the orchestrator's fixed observation window
# (same for every scenario/SSP so the offline damage integrals are comparable).
GOAL_X="${GOAL_X:-30.0}"                 # ~150 s traverse at burger top speed
RUN_DURATION_S="${RUN_DURATION_S:-200}"  # fixed window: baseline completes; the worst-case
                                         # (SSP=60) stall+recovery stays mostly inside it
ATTACK_DELAY_S="${ATTACK_DELAY_S:-10}"   # travel time before the attack (swarm mid-traverse)

# Formation cohesion / goal gains (passed through to every robot via .env).
# k_form >> k_att makes the formation STIFF: it resists shape distortion, so the
# spin-freeze attack only stalls progress while the shape stays rigid (FE blind).
# Softening k_form lets the frozen anchor STRETCH the formation by ~k_att/k_form,
# so formation error grows during the stall -> ∫FE dt scales with SSP+B and the
# FE-vs-time plot shows a high-FE plateau whose width grows with detection latency.
K_FORM="${K_FORM:-3.0}"                  # 3.0 = stiff (shape-rigid); try 1.0/0.5 to expose FE damage
K_ATT="${K_ATT:-0.8}"

COOLDOWN_S="${COOLDOWN_S:-60}"
GOAL_TIMEOUT_S="${GOAL_TIMEOUT_S:-360}"      # max wait for "Goal published" after start.sh
DONE_TIMEOUT_S="${DONE_TIMEOUT_S:-700}"      # max wait for events-<tag>.json after goal
GPU="${GPU:-on}"
DRY_RUN="false"

IMAGE="${IMAGE:-turtlebot3-gazebo}"          # image with rosbag2_py for offline metrics

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reps)            REPS="$2"; shift 2 ;;
    --rep-start)       REP_START="$2"; shift 2 ;;
    --ssp-list)        SSP_LIST="$2"; shift 2 ;;
    --scenarios)       SCENARIOS="$2"; shift 2 ;;
    --goal-x)          GOAL_X="$2"; shift 2 ;;
    --run-duration)    RUN_DURATION_S="$2"; shift 2 ;;
    --attack-delay)    ATTACK_DELAY_S="$2"; shift 2 ;;
    --k-form)          K_FORM="$2"; shift 2 ;;
    --k-att)           K_ATT="$2"; shift 2 ;;
    --gpu)             GPU="$2"; shift 2 ;;
    --data-subdir)     DATA_SUBDIR="$2"; shift 2 ;;
    --dry-run)         DRY_RUN="true"; shift ;;
    -h|--help)         sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "❌ unknown arg: $1" >&2; exit 1 ;;
  esac
done

# FDIR must be set AFTER CLI parsing so --data-subdir takes effect before
# any code that uses $FDIR (inject records, wait_run_done, mkdir, etc.).
FDIR="$SCRIPT_DIR/experiments/data/$DATA_SUBDIR"

log() { echo -e "[$(date +%H:%M:%S)] $*"; }
upsert(){ local k="$1" v="$2"; if grep -qE "^[[:space:]]*$k=" .env; then sed -i "s|^[[:space:]]*$k=.*|$k=$v|g" .env; else echo "$k=$v" >> .env; fi; }

# ── Run primitives ────────────────────────────────────────────────────────────

# Wait until the formation orchestrator logs "Goal published" (mission has begun).
wait_goal_published() {
  local deadline=$(( $(date +%s) + GOAL_TIMEOUT_S ))
  while (( $(date +%s) < deadline )); do
    if docker ps --format '{{.Names}}' | grep -qx orchestrator; then
      docker logs orchestrator 2>&1 | grep -q "Goal published" && return 0
    fi
    sleep 2
  done
  return 1
}

# Wait until events-<tag>.json exists (orchestrator fixed window elapsed).
wait_run_done() {
  local tag="$1"
  local ev="$FDIR/events-${tag}.json"
  local deadline=$(( $(date +%s) + DONE_TIMEOUT_S ))
  while (( $(date +%s) < deadline )); do
    [[ -f "$ev" ]] && return 0
    sleep 2
  done
  return 1
}

# Worst-case anchor: block until the victim's prover sidecar logs a fresh
# attestation measurement after since_s. Injecting right after -> detection = SSP+B.
wait_final_digest() {
  local since_s="$1" timeout_s="$2"
  local sidecar="${VICTIM}-sidecar"
  local deadline=$(( $(date +%s) + timeout_s ))
  while (( $(date +%s) < deadline )); do
    if docker logs --since "$since_s" "$sidecar" 2>&1 | \
       grep -qE "\[Prover\].*(Final digest|Rolling-hash digest)"; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

inject_attack() {
  local tag="$1" anchored="${2:-false}"
  log "💥 injecting spin-attack tamper on $VICTIM (formation_agent, constant-turn fault)"
  ./tools/tamper.sh "$VICTIM" --target formation_agent || { log "⚠️ tamper failed"; return 1; }
  # Capture T0 immediately after tamper.sh returns (byte already written).
  # Millisecond precision; anchored=true means injection followed a Final-digest
  # boundary (worst-case: detection ≈ SSP + B). Consistent with T0 in
  # run_experiments_and_collect_tamper_detection.py.
  local t_wall; t_wall="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
  # Ensure $FDIR is host-user-owned before writing the inject record; containers
  # running as root may have taken ownership of the directory during the run.
  docker run --rm -v "$FDIR":/d alpine chown "$(id -u):$(id -g)" /d 2>/dev/null || true
  ( printf '{"tag":"%s","victim":"%s","attack_delay_s":%s,"t_inject_wall":"%s","anchored":%s}\n' \
      "$tag" "$VICTIM" "$ATTACK_DELAY_S" "$t_wall" "$anchored" \
      > "$FDIR/inject-${tag}.json" \
  ) 2>/dev/null
  # The inject record holds t_inject_wall, which is NOT in the bag — if it is lost,
  # the headline IFE metric (∫FE over [inject, detect]) is unrecoverable for this
  # run. Verify it landed and warn prominently rather than failing silently.
  if [[ -s "$FDIR/inject-${tag}.json" ]]; then
    log "📝 inject record written (t_inject_wall=$t_wall)"
  else
    log "❌❌ FAILED to write inject record for $tag — IFE will be UNRECOVERABLE. Wall time was: $t_wall"
  fi
  # The process MUST stay alive for the corrupted .text to be measured -> FAILURE.
  if docker exec "$VICTIM" pgrep -a formation_agent >/dev/null 2>&1; then
    log "✅ $VICTIM formation_agent still alive (steering fault active)"
  else
    log "❌ $VICTIM formation_agent is GONE — tamper crashed it (no FAILURE will record)"
  fi
}

# Extract offline metrics from the recorded bag (after teardown flushed it).
extract_metrics() {
  local tag="$1"
  local bag="$FDIR/bags/${tag}"
  local ev="$FDIR/events-${tag}.json"
  if [[ ! -d "$bag" ]]; then
    log "⚠️ no bag for $tag at $bag — skipping metrics"; return 1
  fi
  log "📈 extracting metrics for $tag"
  docker run --rm --user root -v "$SCRIPT_DIR:/w" -w /w "$IMAGE" bash -lc \
    "source /opt/ros/humble/setup.bash && python3 tools/formation_metrics.py \
       --bag experiments/data/${DATA_SUBDIR}/bags/${tag} \
       --events experiments/data/${DATA_SUBDIR}/events-${tag}.json \
       --out-dir experiments/data/${DATA_SUBDIR}" 2>&1 | grep -vE "rosbag2_storage|INFO" | tail -20
}

# one_run <tag> <ssp> <mitigation on|off> <attack true|false>
one_run() {
  local tag="$1" ssp="$2" mit="$3" attack="$4"
  log "══════════════════════════════════════════════════════════════"
  log "▶ RUN  tag=$tag  ssp=$ssp  mitigation=$mit  attack=$attack"

  # Campaign-controlled mission geometry / window (start.sh does not set these).
  upsert GOAL_X "$GOAL_X"; upsert GOAL_Y 0.0; upsert RUN_DURATION_S "$RUN_DURATION_S"
  # Cohesion/goal gains — softer k_form lets the anchor stretch the formation (FE damage).
  upsert K_FORM "$K_FORM"; upsert K_ATT "$K_ATT"
  # Data subdirectory — must be in .env so docker-compose.formation.yml picks it up
  # for both the orchestrator LOG_DIR and the bag-recorder volume mount.
  upsert FORMATION_DATA_SUBDIR "$DATA_SUBDIR"

  # Remove any stale events file left by a previous run with the same tag
  # (e.g. a validation run). If the file already exists, wait_run_done would
  # return immediately and stop.sh would kill the bag-recorder after ~2 s.
  rm -f "$FDIR/events-${tag}.json"
  # Pre-create the bags directory so docker-compose mounts it as the right owner.
  mkdir -p "$FDIR/bags"

  if [[ "$DRY_RUN" == "true" ]]; then
    log "   (dry-run) ./start.sh --robots 4 --formation --auto \
--mitigation $mit --run-tag $tag --ssp $ssp $([[ "$GPU" == "on" ]] && echo --gpu)"
    return 0
  fi

  local gpu_flag=(); [[ "$GPU" == "on" ]] && gpu_flag=(--gpu)
  # Pass FORMATION_DATA_SUBDIR as a shell env var so start.sh's mkdir and log
  # message use the correct subdir (docker-compose.formation.yml already reads it
  # from .env, which was updated by upsert above).
  FORMATION_DATA_SUBDIR="$DATA_SUBDIR" ./start.sh --robots 4 --formation --auto \
    --mitigation "$mit" --run-tag "$tag" --ssp "$ssp" \
    "${gpu_flag[@]}"

  log "⏳ waiting for goal published (mission start)…"
  if ! wait_goal_published; then
    log "❌ goal never published for $tag — tearing down and skipping"
    ./stop.sh >/dev/null 2>&1 || true
    return 1
  fi
  log "🟢 mission started"

  # Verify the bag-recorder is still alive — it can crash on cold starts if
  # topics aren't yet publishing. Better to abort loudly than collect an empty bag.
  if ! docker ps --format '{{.Names}}' | grep -qx bag-recorder; then
    log "❌ bag-recorder is NOT running — aborting run $tag (no data would be captured)"
    ./stop.sh >/dev/null 2>&1 || true
    return 1
  fi
  log "📼 bag-recorder confirmed running"

  if [[ "$attack" == "true" ]]; then
    log "⏳ letting the swarm travel ${ATTACK_DELAY_S}s before the attack…"
    sleep "$ATTACK_DELAY_S"
    local since_s; since_s="$(date +%s)"
    local digest_timeout=$(( ssp / 1000 + 15 ))
    log "⏳ waiting for ${VICTIM}-sidecar Final digest (worst-case anchor, timeout ${digest_timeout}s)…"
    local anchored="false"
    if wait_final_digest "$since_s" "$digest_timeout"; then
      log "🔔 attestation boundary — injecting NOW (expected detection ≈ ${ssp}ms + B)"
      anchored="true"
    else
      log "⚠️ no Final digest within ${digest_timeout}s — injecting unanchored"
    fi
    inject_attack "$tag" "$anchored"
  fi

  log "⏳ waiting for run window to finish (events-${tag}.json)…"
  if wait_run_done "$tag"; then
    log "🏁 run finished: $(tr -d '\n' < "$FDIR/events-${tag}.json")"
  else
    log "⚠️ timed out waiting for events-${tag}.json"
  fi

  log "🧹 teardown (flushes the bag)"
  ./stop.sh >/dev/null 2>&1 || ./stop.sh

  extract_metrics "$tag"
  log "🧊 cooldown ${COOLDOWN_S}s"
  sleep "$COOLDOWN_S"
}

# ── Campaign ──────────────────────────────────────────────────────────────────
mkdir -p "$FDIR"
# The orchestrator/recorder containers write events/bags as root, leaving the
# data dir root-owned; take ownership so the host-side script can also write its
# inject-<tag>.json records here (root containers can still write inside).
docker run --rm -v "$FDIR":/d alpine chown -R "$(id -u):$(id -g)" /d 2>/dev/null || true
log "Formation campaign: scenarios=[$SCENARIOS] reps=$REPS victim=$VICTIM"
log "          goal_x=$GOAL_X window=${RUN_DURATION_S}s attack_delay=${ATTACK_DELAY_S}s ssp_list=[$SSP_LIST] gpu=${GPU}"
log "          k_form=$K_FORM k_att=$K_ATT (lower k_form -> softer formation -> FE damage scales with stall)"

for rep in $(seq "$REP_START" $(( REP_START + REPS - 1 )) ); do
  for scn in $SCENARIOS; do
    case "$scn" in
      baseline)
        one_run "baseline-r${rep}" "$BASELINE_SSP" on false ;;
      unmitigated)
        one_run "unmitigated-ssp${UNMIT_SSP}-r${rep}" "$UNMIT_SSP" off true ;;
      mitigated)
        for ssp in $SSP_LIST; do
          one_run "mitigated-ssp${ssp}-r${rep}" "$ssp" on true
        done ;;
      *) log "⚠️ unknown scenario '$scn' — skipping" ;;
    esac
  done
done

log "✅ Formation campaign complete. Outputs in $FDIR/"
ls -1 "$FDIR"/metrics-*.json 2>/dev/null || true
