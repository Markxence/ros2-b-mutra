#!/usr/bin/env bash
# run_campaign.sh — automate the swarm mission-impact experiment campaign.
#
# Runs the three scenarios that produce the paper's "% completed vs time" figure,
# each repeated REPS times, tearing the stack down fresh between runs:
#
#   baseline            no attack, mitigation on   -> clean 100% reference
#   unmitigated         attack robot, MITIGATION=off -> progress plateaus < 100%
#   mitigated (sweep)   attack robot, MITIGATION=on, SSP in {SSP_LIST}
#                       -> stalls then recovers to 100% (stall width ~ detection latency)
#
# Per run the controller:
#   1. ./start.sh ... --run-tag <tag>           (bring up a fresh stack)
#   2. wait for the orchestrator "clock started" (mission begin)
#   3. (attack runs) wait until the victim has completed ATTACK_AT_K waypoints
#      (progress pre-condition), then sync to the next attestation measurement
#      boundary ("Final digest") and inject immediately — worst-case anchoring
#      so detection latency = SSP + B for every trial. This gives SSP-proportional
#      stall widths in the mission curve, isolating detection interval as the
#      independent variable across the mitigated sweep.
#   4. wait for summary-<tag>.json (mission complete / timeout)
#   5. ./stop.sh   (teardown), cooldown, next run
#
# Each run yields experiments/data/mission/{mission-<tag>.csv, summary-<tag>.json}.
# All other metrics (attestation times, detection latency, docker/chain stats)
# are collected by the existing monitoring and are intentionally NOT this
# controller's concern — it only drives mission-impact data.
#
# Usage:
#   ./tools/run_campaign.sh [--reps N] [--rep-start S] [--victim robotN]
#                           [--attack-at-k K] [--ssp-list "10000 20000 30000"]
#                           [--scenarios "..."] [--mission-timeout S]
#                           [--world NAME] [--gpu on|off]
#                           [--dry-run]
#
# Run it in small pieces (recommended — one thing at a time so you can inspect):
#   # one baseline run
#   ./tools/run_campaign.sh --scenarios baseline --reps 1
#   # one unmitigated (plateau) run
#   ./tools/run_campaign.sh --scenarios unmitigated --reps 1
#   # one mitigated run at a single SSP you want to play with
#   ./tools/run_campaign.sh --scenarios mitigated --ssp-list 20000 --reps 1
#   # add a 2nd/3rd repetition later WITHOUT overwriting r1 (accumulate):
#   ./tools/run_campaign.sh --scenarios baseline --reps 1 --rep-start 2
#
# There is always exactly ONE victim (default robot3); the swarm size is fixed
# at 4. Defaults below — override any via flags or env.

set -uo pipefail
# NOTE: default IFS (space/tab/newline) is required so space-separated
# SCENARIOS and SSP_LIST word-split correctly in the loops below.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# ── Tunables ──────────────────────────────────────────────────────────────────
REPS="${REPS:-3}"
REP_START="${REP_START:-1}"   # first repetition index (so reps can be accumulated
                              # across separate invocations: e.g. --rep-start 4 --reps 1)
VICTIM="${VICTIM:-robot3}"
# Fault injection is triggered on mission PROGRESS, not wall-clock time: the
# tamper fires the moment the victim has completed exactly ATTACK_AT_K of its
# waypoints. This makes the orphaned-waypoint set (and therefore the
# redistribution travel cost) identical across every repetition and SSP value,
# so the ONLY thing that varies across the sweep is the detection stall —
# isolating detection latency as the independent variable. A fixed time delay
# would let Nav2 jitter change how much work is orphaned, confounding the runs.
ATTACK_AT_K="${ATTACK_AT_K:-3}"             # progress pre-condition: wait until victim completes K
                                            # waypoints before watching for attestation boundary.
                                            # K=3 ensures robot3 is mid-patrol before the freeze.
WORLD="${WORLD:-swarm_arena}"               # Gazebo world for the campaign
SSP_LIST="${SSP_LIST:-10000 20000 30000}"   # mitigated sweep (detection-interval sweep)
BASELINE_SSP="${BASELINE_SSP:-10000}"
UNMIT_SSP="${UNMIT_SSP:-10000}"
SCENARIOS="${SCENARIOS:-baseline unmitigated mitigated}"
MISSION_TIMEOUT_S="${MISSION_TIMEOUT_S:-400}"   # orchestrator give-up; bounds the unmitigated plateau
                                                # (32-waypoint arena mission: baseline ~3min,
                                                #  mitigated ~4-5min, so keep this generous)
COOLDOWN_S="${COOLDOWN_S:-60}"   # let the host fully settle between batched runs (reduces load-jitter outliers)
CLOCK_TIMEOUT_S="${CLOCK_TIMEOUT_S:-360}"   # max wait for mission to start after start.sh
PROGRESS_TIMEOUT_S="${PROGRESS_TIMEOUT_S:-180}"  # max wait for victim to reach K waypoints
DONE_TIMEOUT_S="${DONE_TIMEOUT_S:-600}"     # max wait for summary after mission start
GPU="${GPU:-on}"                            # GPU-accelerate Gazebo (keeps RTF≈1; --gpu off to disable)
RTF_MIN="${RTF_MIN:-0.9}"                   # runs below this real-time factor are flagged INVALID (host overloaded)
DRY_RUN="false"

MISSION_DIR="$SCRIPT_DIR/experiments/data/mission"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reps)            REPS="$2"; shift 2 ;;
    --rep-start)       REP_START="$2"; shift 2 ;;
    --victim)          VICTIM="$2"; shift 2 ;;
    --attack-at-k)     ATTACK_AT_K="$2"; shift 2 ;;
    --ssp-list)        SSP_LIST="$2"; shift 2 ;;
    --scenarios)       SCENARIOS="$2"; shift 2 ;;
    --mission-timeout) MISSION_TIMEOUT_S="$2"; shift 2 ;;
    --world)           WORLD="$2"; shift 2 ;;
    --gpu)             GPU="$2"; shift 2 ;;
    --dry-run)         DRY_RUN="true"; shift ;;
    -h|--help)         sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "❌ unknown arg: $1" >&2; exit 1 ;;
  esac
done

log() { echo -e "[$(date +%H:%M:%S)] $*"; }

# ── Run primitives ────────────────────────────────────────────────────────────

# Wait until the orchestrator logs "clock started" (mission has begun).
wait_clock_started() {
  local deadline=$(( $(date +%s) + CLOCK_TIMEOUT_S ))
  while (( $(date +%s) < deadline )); do
    if docker ps --format '{{.Names}}' | grep -qx orchestrator; then
      if docker logs orchestrator 2>&1 | grep -q "clock started"; then
        return 0
      fi
    fi
    sleep 2
  done
  return 1
}

# Wait until the summary file for this tag exists (mission complete / timeout).
wait_mission_done() {
  local tag="$1"
  local summary="$MISSION_DIR/summary-${tag}.json"
  local deadline=$(( $(date +%s) + DONE_TIMEOUT_S ))
  while (( $(date +%s) < deadline )); do
    [[ -f "$summary" ]] && return 0
    # If the orchestrator container vanished without a summary, bail.
    docker ps --format '{{.Names}}' | grep -qx orchestrator || { [[ -f "$summary" ]] && return 0; }
    sleep 2
  done
  return 1
}

# Wait until the victim has completed >= K waypoints (read from the orchestrator
# CSV, the authoritative completed count). Column layout:
#   1 t_rel_s | 2 wall_iso | 3 pct | 4 completed | 5 total
#   6 done_robot1 | 7 done_robot2 | ... | (5+i) done_robot<i> | healthy | event
wait_victim_progress() {
  local tag="$1" k="$2"
  local csv="$MISSION_DIR/mission-${tag}.csv"
  local vnum="${VICTIM#robot}"      # robot3 -> 3
  local col=$(( 5 + vnum ))         # done_robot<vnum>
  local deadline=$(( $(date +%s) + PROGRESS_TIMEOUT_S ))
  while (( $(date +%s) < deadline )); do
    if [[ -f "$csv" ]]; then
      local d
      d="$(tail -1 "$csv" | cut -d, -f"$col")"
      if [[ "$d" =~ ^[0-9]+$ ]] && (( d >= k )); then
        return 0
      fi
    fi
    sleep 1
  done
  return 1
}

# Worst-case anchor: block until the victim's prover sidecar logs a fresh
# attestation measurement after since_s (unix epoch seconds).
# Pattern mirrors run_experiments_and_collect_tamper_detection.py PROVER_MEASURE_RE.
# Injecting immediately after this line gives: detection = SSP + B.
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
  local tag="$1" mit="$2"
  log "💥 injecting tamper on $VICTIM (mission_agent, true-freeze RET)"
  ./tools/tamper.sh "$VICTIM" --target mission_agent || { log "⚠️ tamper failed"; return 1; }

  # Record the injection wall-clock time so analysis can compute detection latency:
  #   detection_latency = t_detect_rel (from summary failure_events)
  #                     - t_inject_rel (t_inject_wall - t_start_wall, from CSV mission_start row)
  local t_wall
  t_wall="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"tag":"%s","victim":"%s","attack_at_k":%s,"t_inject_wall":"%s"}\n' \
    "$tag" "$VICTIM" "$ATTACK_AT_K" "$t_wall" \
    > "$MISSION_DIR/inject-${tag}.json"
  log "📝 injection record: $MISSION_DIR/inject-${tag}.json  ($t_wall)"

  # Soft-freeze check: the process MUST stay alive for the .text FAILURE to be measured.
  if docker exec "$VICTIM" pgrep -a mission_agent >/dev/null 2>&1; then
    log "✅ $VICTIM mission_agent still alive (soft-freeze confirmed)"
  else
    log "❌ $VICTIM mission_agent is GONE — tamper crashed it (no FAILURE will be recorded)"
  fi

  # Mitigated only: isolate the now-immobilized robot by removing its Gazebo body.
  # This is experimental hygiene — it does NOT affect detection (the tampered
  # .text is still attested in the still-running container) or the reassignment
  # logic. It only removes the inert body so the rescuer's open-loop path is
  # collision-free, eliminating the stochastic contact dynamics that otherwise
  # inflate the mitigated run's distance/time. Framed in the paper as "the
  # immobilized robot is isolated from the workspace as part of mitigation."
  if [[ "$mit" == "on" ]]; then
    local entity="${VICTIM}_burger"   # spawn entity name = <ns>_<TURTLEBOT3_MODEL>
    if docker exec gazebo-server bash -lc \
        "source /opt/ros/humble/setup.bash; source /home/agent/ros2_ws/install/setup.bash; \
         ros2 service call /delete_entity gazebo_msgs/srv/DeleteEntity \"{name: '${entity}'}\"" \
        >/dev/null 2>&1; then
      log "🧹 isolated $VICTIM (removed Gazebo entity $entity)"
    else
      log "⚠️ could not remove Gazebo entity $entity (rescuer may contact the frozen body)"
    fi
  fi
}

# one_run <tag> <ssp> <mitigation on|off> <attack true|false>
one_run() {
  local tag="$1" ssp="$2" mit="$3" attack="$4"
  log "══════════════════════════════════════════════════════════════"
  log "▶ RUN  tag=$tag  ssp=$ssp  mitigation=$mit  attack=$attack"
  if [[ "$DRY_RUN" == "true" ]]; then
    log "   (dry-run) MISSION_TIMEOUT_S=$MISSION_TIMEOUT_S ./start.sh --robots 4 --nav2 \
--world $WORLD --attest-mission-agent --mission --auto \
--mitigation $mit --run-tag $tag --ssp $ssp $([[ "$GPU" == "on" ]] && echo --gpu)"
    return 0
  fi

  local gpu_flag=()
  [[ "$GPU" == "on" ]] && gpu_flag=(--gpu)
  MISSION_TIMEOUT_S="$MISSION_TIMEOUT_S" ./start.sh \
    --robots 4 --nav2 --world "$WORLD" \
    --attest-mission-agent --mission --auto \
    --mitigation "$mit" --run-tag "$tag" --ssp "$ssp" \
    "${gpu_flag[@]}"

  log "⏳ waiting for mission to start (clock)…"
  if ! wait_clock_started; then
    log "❌ mission never started for $tag — tearing down and skipping"
    ./stop.sh >/dev/null 2>&1 || true
    return 1
  fi
  log "🟢 mission started"

  if [[ "$attack" == "true" ]]; then
    log "⏳ waiting for $VICTIM to complete $ATTACK_AT_K waypoint(s) (progress pre-condition)…"
    if wait_victim_progress "$tag" "$ATTACK_AT_K"; then
      log "🎯 $VICTIM reached $ATTACK_AT_K waypoints — syncing to attestation cycle boundary"
    else
      log "⚠️ $VICTIM never reached $ATTACK_AT_K waypoints within ${PROGRESS_TIMEOUT_S}s — syncing anyway"
    fi

    # Worst-case anchor: inject right after the next "Final digest" from the
    # victim's sidecar (the start of the inter-cycle SSP sleep). The just-
    # completed measurement is clean; the tamper is first sampled one full SSP
    # period later. Detection latency = SSP + B (blockchain confirmation time),
    # giving SSP-proportional stall widths that are clearly separated in the plot.
    local since_s
    since_s="$(date +%s)"
    local digest_timeout=$(( ssp / 1000 + 15 ))
    log "⏳ waiting for ${VICTIM}-sidecar Final digest (worst-case anchor, timeout ${digest_timeout}s)…"
    if wait_final_digest "$since_s" "$digest_timeout"; then
      log "🔔 attestation boundary — injecting NOW (expected detection ≈ ${ssp}ms + B)"
    else
      log "⚠️ no Final digest within ${digest_timeout}s — injecting unanchored"
    fi
    inject_attack "$tag" "$mit"
  fi

  log "⏳ waiting for mission to finish (summary-${tag}.json)…"
  if wait_mission_done "$tag"; then
    log "🏁 finished: $(cat "$MISSION_DIR/summary-${tag}.json" | tr -d '\n')"
  else
    log "⚠️ timed out waiting for summary-${tag}.json"
  fi

  # RTF validity gate (read-only here). The ORCHESTRATOR computes effective_rtf =
  # sim_elapsed / wall_elapsed from Gazebo's /clock and writes valid_run into the
  # summary using RTF_MIN (exported below so compose passes it through). A run
  # where the simulator could not sustain real-time is UNPHYSICAL for this study:
  # the mission moves in sim-time while blockchain detection runs in wall-time, so
  # they only compose into a meaningful stall at RTF≈1. We only READ the verdict
  # here (the summary is root-owned by the container) — invalid runs should be
  # discarded and repeated.
  if [[ -f "$MISSION_DIR/summary-${tag}.json" ]]; then
    local verdict
    verdict="$(python3 -c "
import json
with open('$MISSION_DIR/summary-${tag}.json') as f: d=json.load(f)
rtf=d.get('effective_rtf'); v=d.get('valid_run')
if v is None: print('UNKNOWN', rtf)
else: print(('VALID' if v else 'INVALID'), rtf)
")"
    case "$verdict" in
      INVALID*) log "🚫 INVALID RUN: effective RTF=${verdict#INVALID } < $RTF_MIN for $tag — DISCARD and re-run this rep" ;;
      VALID*)   log "📊 effective RTF for $tag: ${verdict#VALID } (valid)" ;;
      *)        log "⚠️ no /clock seen — effective RTF unknown for $tag; RTF gate skipped" ;;
    esac
  fi

  log "🧹 teardown"
  ./stop.sh >/dev/null 2>&1 || ./stop.sh
  sleep "$COOLDOWN_S"
}

# ── Campaign ──────────────────────────────────────────────────────────────────
# Export RTF_MIN so docker compose (via start.sh) passes it into the orchestrator
# container, which is the single source of truth for the valid_run verdict.
export RTF_MIN
mkdir -p "$MISSION_DIR"
log "Campaign: scenarios=[$SCENARIOS] reps=$REPS victim=$VICTIM attack_at_k=${ATTACK_AT_K} completed"
log "          world=$WORLD ssp_list=[$SSP_LIST] mission_timeout=${MISSION_TIMEOUT_S}s gpu=${GPU}"

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

log "✅ Campaign complete. Outputs in $MISSION_DIR/"
ls -1 "$MISSION_DIR"/summary-*.json 2>/dev/null || true
