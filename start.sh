#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

# --- Paths ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
SECAAS_URL="http://localhost:8000"

# One Compose project name for ALL stacks
PROJECT_NAME="ros2-b-mutra"

# Remote host constants
REMOTE1_HOST="10.5.1.20"
REMOTE1_USER="nextnet"
REMOTE1_LIMIT=64
REMOTE2_HOST=""          # placeholder — set when a second remote host is available
REMOTE2_USER="nextnet"
REMOTE2_LIMIT=128
REMOTE_DIR="~/ros2-b-mutra"

# d-mutra's IP reachable from remote hosts — used only in remote mode so that
# sidecars on remote machines know where to reach the Besu validators on d-mutra.
# The quorum compose file no longer binds to this IP directly; it uses "21001:8545"
# (all-interfaces) so no LOCAL_IP export is needed for docker compose.
LOCAL_IP="10.5.99.99"

# Ignore orphan warnings when using multiple compose files for the same project
export COMPOSE_IGNORE_ORPHANS=1

run_remote() {
  local host="$1"
  local user="$2"
  shift 2
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$user@$host" "$@"
}

remote_compose_up() {
  local host="$1"
  local user="$2"
  local compose_file="$3"
  local retries=3
  local attempt
  for ((attempt = 1; attempt <= retries; attempt++)); do
    if run_remote "$host" "$user" \
        "cd $REMOTE_DIR && COMPOSE_IGNORE_ORPHANS=1 docker compose -p $PROJECT_NAME --project-directory $REMOTE_DIR -f $compose_file up -d"; then
      return 0
    fi
    if (( attempt < retries )); then
      echo "⚠️  Remote compose failed (attempt $attempt/$retries), retrying in 5s..."
      sleep 5
    fi
  done
  return 1
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [--robots N] [--remote] [--auto] [--export] [--startup] [--wait-tx]
                        [--ssp N] [--cpu-limit X] [--contract rr|lv]
                        [--iterq K] [--no-bootstrap]
                        [--attest-gzserver]
                        [--no-wait-result]

Options:
  --robots N     Number of robots to deploy (default: 4, max: $REMOTE2_LIMIT).
  --remote       Remote deployment mode: d-mutra hosts only Besu+SECaaS+monitoring;
                 all robots+sidecars run on remote host(s) (remote1 up to $REMOTE1_LIMIT robots,
                 remote2 for $((REMOTE1_LIMIT+1))–$REMOTE2_LIMIT robots).
                 Default (local mode): everything runs on d-mutra.
  --auto         Set AUTO_START=TRUE
  --export       Set EXPORT_RESULTS=TRUE
  --wait-tx      Set WAIT_FOR_TX_CONFIRMATIONS=TRUE
  --startup      Set ONE_SHOT=TRUE
  --ssp N        Attestation interval in milliseconds — sets ATTESTATION_INTERVAL_MS=N (default: 10000).
                 Use 0 for back-to-back measurements with no sleep (bounded only by CPU_LIMIT).
  --cpu-limit X  Sidecar CPU limit fraction — sets CPU_LIMIT=X in .env and compose files (default: 0.4)
  --contract rr|lv  Smart contract variant (default: lv; rr = round-robin verifier election)
  --iterq K      IterQ rolling-hash threshold: number of fresh measurements folded into
                 one on-chain attestation. K=1 = single-shot. Range: 1..10000 (default: 1)
  --no-cpu-limit Remove the CPU cgroup cap from sidecar containers entirely (uncapped).
                 Mutually exclusive with --cpu-limit.
                 Saves CPU_LIMIT=none to .env; outputs files tagged cpuNC.
  --no-bootstrap Skip the automatic reference-measurements bootstrap step. By default,
                 after the stack is up, start.sh captures real refs from robot1's sidecar
                 via /digest, syncs them to the SECaaS DB, and resets the on-chain chain
                 so attestations close as SUCCESS rather than FAILURE on placeholder refs.
                 Pass this flag if you deliberately want the placeholder behaviour.
  --attest-gzserver  Local 4-robot Gazebo mode: all sidecars attest the gazebo-server
                 process instead of robot_state_publisher.
  --attest-mission-agent  Sidecars attest the mission_agent process (swarm mission
                 experiments). Requires --nav2. Mutually exclusive with --attest-gzserver.
  --mission      Bring up the orchestrator container to run the waypoint mission
                 (local Gazebo + --nav2 only, N≤4). Reads MISSION_FILE.
  --formation    Formation experiment (local Gazebo, N≤4): launch formation_agent
                 on every robot (decentralized APF over neighbor odometry), spawn
                 the swarm in a square, attest formation_agent, and bring up the
                 formation orchestrator. Mutually exclusive with --nav2/--mission.
  --formation-scale X  Formation half-diagonal d (m), square-corner spawn distance
                 (default: 2.0). Must match the orchestrator's FORMATION_SCALE.
  --mitigation on|off  Orchestrator reassigns a compromised robot's waypoints on
                 attestation FAILURE (on) or only records the failure (off). Default: on.
  --run-tag TAG  Tag for the orchestrator output files (mission-<TAG>.csv). Default: run.
  --no-wait-result  Set WAIT_FOR_VERIFICATION_RESULT=FALSE (fire-and-forget mode)
  --gpu          Enable NVIDIA GPU access in the gazebo-server container
                 (Gazebo mode only: N≤4). Adds a deploy.resources.reservations
                 device block for the nvidia driver. Requires the NVIDIA Container
                 Toolkit on the host.
  --vnc          Add a gazebo-vnc container on ros-net (Gazebo mode only: N≤4).
                 Exposes the Gazebo web VNC viewer at http://localhost:6080.
                 Build the image first: cd dockerfiles/gazebo-vnc && docker build -t gazebo-vnc .
  --world NAME   Gazebo world to load (Gazebo mode only: N≤4). Default: empty.
                 Available: empty, turtlebot3_world, swarm_arena, house, dqn1, dqn2, dqn3, dqn4
  --nav2         Enable Nav2 autonomous navigation stack on every robot container.
                 Sets ENABLE_NAV2=true. Requires the turtlebot3-gazebo image to be
                 rebuilt after adding ros-humble-navigation2 to the Dockerfile.
  -h|--help      Show this help

EOF
}

# Default values
N_ROBOTS_VAL="2"
AUTO_START_VAL="FALSE"
EXPORT_RESULTS_VAL="FALSE"
WAIT_TX_VAL="FALSE"
ONE_SHOT_VAL="FALSE"
DEPLOY_MODE="local"
SSP_VAL="10000"
CPU_LIMIT_VAL="0.4"
NO_CPU_LIMIT_VAL="FALSE"
CPU_LIMIT_EXPLICIT="FALSE"
VARIANT_VAL="lv"
CONTRACT_VAL="AttestationManagerLV"
ITERQ_VAL="1"
BOOTSTRAP_REFS="TRUE"
WAIT_RESULT_VAL="TRUE"
ATTEST_GZSERVER_VAL="FALSE"
GPU_VAL="FALSE"
VNC_VAL="FALSE"
WORLD_VAL="empty"
NAV2_VAL="FALSE"

GZSERVER_TEXT_SECTION_SIZE="85412"
GZSERVER_TEXT_SECTION_OFFSET="35552"

# mission_agent attest target (self-contained .text; tick() hot loop lives in
# the executable's own .text — see dockerfiles/turtlebot3/src/mission_agent).
ATTEST_MISSION_AGENT_VAL="FALSE"
# Size of the executable R E LOAD segment (readelf -l: memsz 0xc25a1) of the
# cmd_vel mission_agent build; measured from the r-xp mapping base (offset 0).
# Covers .text incl. tick(), so the runtime tamper is detected. Re-derive after
# any mission_agent rebuild: readelf -lW <bin> | awk '/LOAD/&&/R E/'.
MISSION_AGENT_TEXT_SECTION_SIZE="796065"
MISSION_AGENT_TEXT_SECTION_OFFSET="0"

# formation_agent attest target (formation experiment; APF tick() hot loop lives
# in the executable's own .text — see dockerfiles/turtlebot3/src/formation_agent).
# R E LOAD segment memsz 0xf8ccd = 1019085, from the r-xp mapping base (offset 0).
# Re-derive after any formation_agent rebuild: readelf -lW <bin> | awk '/LOAD/&&/R E/'.
FORMATION_AGENT_TEXT_SECTION_SIZE="1019085"
FORMATION_AGENT_TEXT_SECTION_OFFSET="0"

# Orchestrator / mission experiment knobs.
MISSION_VAL="FALSE"
MITIGATION_VAL="true"
RUN_TAG_VAL="run"

# Formation experiment knobs.
FORMATION_VAL="FALSE"
FORMATION_SCALE_VAL="2.0"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --robots)
      N_ROBOTS_VAL="$2"
      if ! [[ "$N_ROBOTS_VAL" =~ ^[1-9][0-9]*$ ]] || (( N_ROBOTS_VAL < 1 || N_ROBOTS_VAL > REMOTE2_LIMIT )); then
        echo "❌ --robots must be an integer between 1 and $REMOTE2_LIMIT (got '$N_ROBOTS_VAL')"
        exit 1
      fi
      shift 2 ;;
    --remote)   DEPLOY_MODE="remote"; shift ;;
    --auto)     AUTO_START_VAL="TRUE"; shift ;;
    --export)   EXPORT_RESULTS_VAL="TRUE"; shift ;;
    --wait-tx)  WAIT_TX_VAL="TRUE"; shift ;;
    --startup)  ONE_SHOT_VAL="TRUE"; shift ;;
    --ssp)
      SSP_VAL="$2"
      if ! [[ "$SSP_VAL" =~ ^(0|[1-9][0-9]*)$ ]]; then
        echo "❌ --ssp must be a non-negative integer in milliseconds (got '$SSP_VAL')"
        exit 1
      fi
      shift 2 ;;
    --cpu-limit)
      CPU_LIMIT_VAL="$2"
      CPU_LIMIT_EXPLICIT="TRUE"
      if ! [[ "$CPU_LIMIT_VAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        echo "❌ --cpu-limit must be a positive number (got '$CPU_LIMIT_VAL')"
        exit 1
      fi
      shift 2 ;;
    --no-cpu-limit) NO_CPU_LIMIT_VAL="TRUE"; shift ;;
    --contract)
      VARIANT_VAL="$2"
      if [[ "$VARIANT_VAL" != "rr" && "$VARIANT_VAL" != "lv" ]]; then
        echo "❌ --contract must be 'rr' or 'lv' (got '$VARIANT_VAL')"
        exit 1
      fi
      shift 2 ;;
    --iterq)
      ITERQ_VAL="$2"
      if ! [[ "$ITERQ_VAL" =~ ^[1-9][0-9]*$ ]] || (( ITERQ_VAL > 10000 )); then
        echo "❌ --iterq must be an integer in 1..10000 (got '$ITERQ_VAL')"
        exit 1
      fi
      shift 2 ;;
    --no-bootstrap)  BOOTSTRAP_REFS="FALSE"; shift ;;
    --attest-gzserver) ATTEST_GZSERVER_VAL="TRUE"; shift ;;
    --attest-mission-agent) ATTEST_MISSION_AGENT_VAL="TRUE"; shift ;;
    --mission)       MISSION_VAL="TRUE"; shift ;;
    --formation)     FORMATION_VAL="TRUE"; shift ;;
    --formation-scale)
      FORMATION_SCALE_VAL="$2"
      if ! [[ "$FORMATION_SCALE_VAL" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        echo "❌ --formation-scale must be a positive number (got '$FORMATION_SCALE_VAL')"; exit 1
      fi
      shift 2 ;;
    --mitigation)
      MITIGATION_VAL="$2"
      if [[ "$MITIGATION_VAL" != "on" && "$MITIGATION_VAL" != "off" ]]; then
        echo "❌ --mitigation must be 'on' or 'off' (got '$MITIGATION_VAL')"; exit 1
      fi
      [[ "$MITIGATION_VAL" == "on" ]] && MITIGATION_VAL="true" || MITIGATION_VAL="false"
      shift 2 ;;
    --run-tag)
      if [[ -z "${2-}" ]]; then echo "❌ --run-tag requires a value"; exit 1; fi
      RUN_TAG_VAL="$2"; shift 2 ;;
    --no-wait-result) WAIT_RESULT_VAL="FALSE"; shift ;;
    --gpu)           GPU_VAL="TRUE"; shift ;;
    --vnc)           VNC_VAL="TRUE"; shift ;;
    --world)
      WORLD_VAL="$2"
      case "$WORLD_VAL" in
        empty|turtlebot3_world|swarm_arena|house|dqn1|dqn2|dqn3|dqn4) ;;
        *) echo "❌ --world must be one of: empty, turtlebot3_world, swarm_arena, house, dqn1, dqn2, dqn3, dqn4 (got '$WORLD_VAL')"; exit 1 ;;
      esac
      shift 2 ;;
    --nav2)          NAV2_VAL="TRUE"; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "❌ Unknown arg: $1"; echo; usage; exit 1 ;;
  esac
done

# --no-cpu-limit and --cpu-limit are mutually exclusive
if [[ "$NO_CPU_LIMIT_VAL" == "TRUE" && "$CPU_LIMIT_EXPLICIT" == "TRUE" ]]; then
  echo "❌ --no-cpu-limit and --cpu-limit are mutually exclusive"
  exit 1
fi

if [[ "$ATTEST_GZSERVER_VAL" == "TRUE" ]] && { [[ "$DEPLOY_MODE" != "local" ]] || [[ "$N_ROBOTS_VAL" != "4" ]]; }; then
  echo "❌ --attest-gzserver is only valid with --robots 4 in local mode"
  exit 1
fi

if [[ "$ATTEST_GZSERVER_VAL" == "TRUE" && "$ATTEST_MISSION_AGENT_VAL" == "TRUE" ]]; then
  echo "❌ --attest-gzserver and --attest-mission-agent are mutually exclusive"
  exit 1
fi

# The mission_agent only runs when Nav2 is enabled (it drives navigate_to_pose),
# so attesting it — and running the orchestrator mission — both require --nav2.
if [[ "$ATTEST_MISSION_AGENT_VAL" == "TRUE" && "$NAV2_VAL" != "TRUE" ]]; then
  echo "❌ --attest-mission-agent requires --nav2 (the mission_agent only runs with Nav2)"
  exit 1
fi

if [[ "$MISSION_VAL" == "TRUE" ]] && { [[ "$NAV2_VAL" != "TRUE" ]] || [[ "$DEPLOY_MODE" != "local" ]] || (( N_ROBOTS_VAL > 4 )); }; then
  echo "❌ --mission requires --nav2 in local Gazebo mode (--robots 1..4)"
  exit 1
fi

# Formation experiment: local Gazebo, N≤4, and mutually exclusive with the patrol
# stack (--nav2 / --mission / --attest-*). It brings up its own attest target
# (formation_agent) and orchestrator (docker-compose.formation.yml).
if [[ "$FORMATION_VAL" == "TRUE" ]]; then
  if [[ "$DEPLOY_MODE" != "local" ]] || (( N_ROBOTS_VAL > 4 )); then
    echo "❌ --formation requires local Gazebo mode (--robots 1..4)"; exit 1
  fi
  if [[ "$NAV2_VAL" == "TRUE" || "$MISSION_VAL" == "TRUE" ]]; then
    echo "❌ --formation is mutually exclusive with --nav2 / --mission"; exit 1
  fi
  if [[ "$ATTEST_GZSERVER_VAL" == "TRUE" || "$ATTEST_MISSION_AGENT_VAL" == "TRUE" ]]; then
    echo "❌ --formation is mutually exclusive with --attest-gzserver / --attest-mission-agent"; exit 1
  fi
fi

if [[ "$GPU_VAL" == "TRUE" ]] && (( N_ROBOTS_VAL > 16 )); then
  echo "❌ --gpu is only valid in Gazebo mode (--robots 1..16)"
  exit 1
fi

if [[ "$VNC_VAL" == "TRUE" ]] && (( N_ROBOTS_VAL > 16 )); then
  echo "❌ --vnc is only valid in Gazebo mode (--robots 1..16)"
  exit 1
fi

if [[ "$WORLD_VAL" != "empty" ]] && (( N_ROBOTS_VAL > 16 )); then
  echo "❌ --world is only valid in Gazebo mode (--robots 1..16)"
  exit 1
fi

# Map user-facing variant to internal contract name
if [[ "$VARIANT_VAL" == "lv" ]]; then
  CONTRACT_VAL="AttestationManagerLV"
else
  CONTRACT_VAL="AttestationManagerRR"
fi


# Validate remote2 requirements before proceeding
if [[ "$DEPLOY_MODE" == "remote" ]] && (( N_ROBOTS_VAL > REMOTE1_LIMIT )) && [[ -z "$REMOTE2_HOST" ]]; then
  echo "❌ N=$N_ROBOTS_VAL > REMOTE1_LIMIT=$REMOTE1_LIMIT but REMOTE2_HOST is not configured in start.sh."
  exit 1
fi

# --- Ensure .env has desired values ---
upsert_env() {
  local key="$1"
  local val="$2"
  if grep -qE "^[[:space:]]*$key=" "$ENV_FILE"; then
    sed -i "s|^[[:space:]]*$key=.*|$key=$val|g" "$ENV_FILE"
  else
    echo "$key=$val" >> "$ENV_FILE"
  fi
}

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Creating missing .env at $ENV_FILE"
  touch "$ENV_FILE"
fi

# If the user wants --auto AND we're going to bootstrap, we MUST boot the
# containers with AUTO_START=FALSE so the prover doesn't fire SendEvidence
# against the placeholder refs in the DB while we're capturing real ones.
# We restore .env to the user's intent and trigger /start via HTTP after
# the bootstrap step finishes. In --no-bootstrap mode (or remote mode),
# AUTO_START is passed through as-is.
WANT_AUTO_START="$AUTO_START_VAL"
EFFECTIVE_AUTO_START="$AUTO_START_VAL"
if [[ "$BOOTSTRAP_REFS" == "TRUE" && "$DEPLOY_MODE" == "local" && "$AUTO_START_VAL" == "TRUE" ]]; then
  EFFECTIVE_AUTO_START="FALSE"   # hold the workers until bootstrap is done
fi

upsert_env "EXPORT_RESULTS"            "$EXPORT_RESULTS_VAL"
upsert_env "AUTO_START"                "$EFFECTIVE_AUTO_START"
upsert_env "WAIT_FOR_TX_CONFIRMATIONS" "$WAIT_TX_VAL"
upsert_env "WAIT_FOR_VERIFICATION_RESULT"  "$WAIT_RESULT_VAL"
upsert_env "ONE_SHOT"                  "$ONE_SHOT_VAL"
upsert_env "N_ROBOTS"                  "$N_ROBOTS_VAL"
upsert_env "COMPOSE_PROJECT_NAME"      "$PROJECT_NAME"
upsert_env "DEPLOY_MODE"               "$DEPLOY_MODE"
upsert_env "ATTESTATION_INTERVAL_MS"  "$SSP_VAL"
if [[ "$NO_CPU_LIMIT_VAL" == "TRUE" ]]; then
  upsert_env "CPU_LIMIT" "none"
else
  upsert_env "CPU_LIMIT" "$CPU_LIMIT_VAL"
fi
upsert_env "ITERQ_THRESHOLD"           "$ITERQ_VAL"
upsert_env "GAZEBO_WORLD"              "$WORLD_VAL"
upsert_env "ENABLE_NAV2"              "$NAV2_VAL"
upsert_env "ENABLE_FORMATION"          "$FORMATION_VAL"
upsert_env "FORMATION_SCALE"           "$FORMATION_SCALE_VAL"
upsert_env "MITIGATION"                "$MITIGATION_VAL"
upsert_env "RUN_TAG"                   "$RUN_TAG_VAL"

# Per-world mission file for the orchestrator (its compose is static, so it
# reads MISSION_FILE from .env). Robot spawns and the Nav2 map are baked per
# world by generate_compose.py (WORLD_SPAWN_POSITIONS / WORLD_MAP), so they need
# no .env entries here. We always upsert MISSION_FILE so switching worlds never
# leaves a stale mission in .env.
if [[ "$WORLD_VAL" == "swarm_arena" ]]; then
  upsert_env "MISSION_FILE" "/app/missions/swarm_arena.json"
else
  upsert_env "MISSION_FILE" "/app/missions/turtlebot3_world.json"
fi

CPU_DISPLAY="$CPU_LIMIT_VAL"
[[ "$NO_CPU_LIMIT_VAL" == "TRUE" ]] && CPU_DISPLAY="none (uncapped)"
echo "✅ .env updated (Robots: $N_ROBOTS_VAL, Mode: $DEPLOY_MODE, Contract: $CONTRACT_VAL, Auto: $AUTO_START_VAL, Export: $EXPORT_RESULTS_VAL, Startup: $ONE_SHOT_VAL, SSP: ${SSP_VAL}ms, CPU: $CPU_DISPLAY, IterQ: $ITERQ_VAL)"

# Regenerate compose files for the selected mode.
# Blockchain host differs per mode:
#   local  — sidecars are on d-mutra, reach validators via host.docker.internal
#   remote — sidecars are on remote machines, reach validators via d-mutra's LAN IP
echo "🔧 Generating compose files for $N_ROBOTS_VAL robot(s) [mode: $DEPLOY_MODE]..."
if [[ "$DEPLOY_MODE" == "remote" ]]; then
  COMPOSE_BLOCKCHAIN_HOST="$LOCAL_IP"
else
  COMPOSE_BLOCKCHAIN_HOST="host.docker.internal"
fi
COMPOSE_FLAGS=(--robots "$N_ROBOTS_VAL" --blockchain-host "$COMPOSE_BLOCKCHAIN_HOST" --mode "$DEPLOY_MODE" --contract "$CONTRACT_VAL" --world "$WORLD_VAL")
[[ "$FORMATION_VAL" == "TRUE" ]] && COMPOSE_FLAGS+=(--formation --formation-scale "$FORMATION_SCALE_VAL")
[[ "$NO_CPU_LIMIT_VAL" == "TRUE" ]] && COMPOSE_FLAGS+=(--no-cpu-limit)
[[ "$ATTEST_GZSERVER_VAL" == "TRUE" ]] && COMPOSE_FLAGS+=(--attest-gzserver)
[[ "$GPU_VAL" == "TRUE" ]] && COMPOSE_FLAGS+=(--gpu)
[[ "$VNC_VAL" == "TRUE" ]] && COMPOSE_FLAGS+=(--vnc)
python3 "$SCRIPT_DIR/generate_compose.py" "${COMPOSE_FLAGS[@]}"
echo

# Reset local EventWatcher checkpoints (SECaaS always on d-mutra)
CHECKPOINT_DIR="$SCRIPT_DIR/checkpoints"
RESET_SCRIPT="$CHECKPOINT_DIR/reset_event_watcher.sh"

if [[ -d "$CHECKPOINT_DIR" && -f "$RESET_SCRIPT" ]]; then
  echo "🧹 Resetting local EventWatcher checkpoints..."
  docker run --rm -v "$CHECKPOINT_DIR":/cp alpine sh -c 'find /cp -name "*.json" -delete'
  echo
else
  echo "⚠️  Skipping local checkpoint reset (missing $RESET_SCRIPT or $CHECKPOINT_DIR)"
  echo
fi

# Reset remote EventWatcher checkpoints so robot sidecars don't start with a
# stale from_block from an old chain (remote checkpoints are not cleared above).
# The files are root-owned (written by Docker containers), so we use a throwaway
# alpine container to delete them — no sudo TTY issues.
if [[ "$DEPLOY_MODE" == "remote" ]]; then
  echo "🧹 Resetting remote EventWatcher checkpoints on $REMOTE1_HOST..."
  run_remote "$REMOTE1_HOST" "$REMOTE1_USER" \
    "mkdir -p $REMOTE_DIR/checkpoints && docker run --rm -v \$(realpath $REMOTE_DIR/checkpoints):/cp alpine sh -c 'find /cp -name \"*.json\" -delete' && echo 'Done.'"
  echo
  if (( N_ROBOTS_VAL > REMOTE1_LIMIT )) && [[ -n "$REMOTE2_HOST" ]]; then
    echo "🧹 Resetting remote EventWatcher checkpoints on $REMOTE2_HOST..."
    run_remote "$REMOTE2_HOST" "$REMOTE2_USER" \
      "mkdir -p $REMOTE_DIR/checkpoints && docker run --rm -v \$(realpath $REMOTE_DIR/checkpoints):/cp alpine sh -c 'find /cp -name \"*.json\" -delete' && echo 'Done.'"
    echo
  fi
fi

# ── Initialize the blockchain network (always on d-mutra) ─────────────────────
cd "$SCRIPT_DIR/blockchain/quorum-test-network"
./run.sh

sleep 10

# ── Deploy the smart contract ──────────────────────────────────────────────────
cd "$SCRIPT_DIR"
deploy_flags=(--rpc_url http://localhost:21001 --chain_id 1337 --contract "$CONTRACT_VAL")
./deploy_sc.sh "${deploy_flags[@]}"

# ── Generate agent config JSONs (config/ and config-dummy/) ───────────────────
# Blockchain host seen by sidecar containers differs per deploy mode.
if [[ "$DEPLOY_MODE" == "remote" ]]; then
  CFG_BLOCKCHAIN_HOST="$LOCAL_IP"
else
  CFG_BLOCKCHAIN_HOST="host.docker.internal"
fi

echo "🔧 Generating agent configs (contract: $CONTRACT_VAL, blockchain-host: $CFG_BLOCKCHAIN_HOST)..."

if [[ "$ATTEST_GZSERVER_VAL" == "TRUE" ]]; then
  _ATTEST_CMD_NAME="gzserver"
  _ATTEST_TEXT_SIZE="$GZSERVER_TEXT_SECTION_SIZE"
  _ATTEST_OFFSET="$GZSERVER_TEXT_SECTION_OFFSET"
elif [[ "$ATTEST_MISSION_AGENT_VAL" == "TRUE" ]]; then
  _ATTEST_CMD_NAME="mission_agent"
  _ATTEST_TEXT_SIZE="$MISSION_AGENT_TEXT_SECTION_SIZE"
  _ATTEST_OFFSET="$MISSION_AGENT_TEXT_SECTION_OFFSET"
elif [[ "$FORMATION_VAL" == "TRUE" ]]; then
  _ATTEST_CMD_NAME="formation_agent"
  _ATTEST_TEXT_SIZE="$FORMATION_AGENT_TEXT_SECTION_SIZE"
  _ATTEST_OFFSET="$FORMATION_AGENT_TEXT_SECTION_OFFSET"
else
  _ATTEST_CMD_NAME="robot_state_publisher"
  _ATTEST_TEXT_SIZE="42223"
  _ATTEST_OFFSET="0"
fi

CONFIG_ARGS=(
  --num-agents 100
  --contract "$CONTRACT_VAL"
  --config-output "$SCRIPT_DIR/config"
  --ref-output "$SCRIPT_DIR/ref-measurements"
  --blockchain-host "$CFG_BLOCKCHAIN_HOST"
  --secaas-host "host.docker.internal"
  --cmd-name "$_ATTEST_CMD_NAME"
  --text-section-size "$_ATTEST_TEXT_SIZE"
  --offset "$_ATTEST_OFFSET"
)
python3 "$SCRIPT_DIR/create_agent_config.py" "${CONFIG_ARGS[@]}"

python3 "$SCRIPT_DIR/create_agent_config.py" \
  --num-agents 100 \
  --contract "$CONTRACT_VAL" \
  --config-output "$SCRIPT_DIR/config-dummy" \
  --blockchain-host "$CFG_BLOCKCHAIN_HOST" \
  --cmd-name dummy_publisher \
  --text-section-size 175521 \
  --offset 5744

echo

# ── SECaaS helper (shared by both deployment branches) ────────────────────────
wait_for_secaas() {
  echo "⏳ Waiting for SECaaS API to be ready at $SECAAS_URL..."
  local max_attempts=30
  local attempt=1
  while (( attempt <= max_attempts )); do
    if curl -s "$SECAAS_URL/" | grep -q "SECaaS running"; then
      echo "✅ SECaaS is UP and running!"
      return 0
    fi
    echo "   (Attempt $attempt/$max_attempts) Still waiting..."
    sleep 2
    (( attempt++ ))
  done
  echo "❌ SECaaS failed to start in time."
  return 1
}

# ══════════════════════════════════════════════════════════════════════════════
if [[ "$DEPLOY_MODE" == "remote" ]]; then
# ── REMOTE MODE ───────────────────────────────────────────────────────────────
# d-mutra: Besu + SECaaS + monitoring only.
# All robots + sidecars run on remote host(s).
  echo "🌐 Remote mode: d-mutra hosts Besu+SECaaS+monitoring; robots+sidecars on remote host(s)"
  echo

  # Sync .env, generated/, config/, and config-dummy/ to remote1
  echo "📤 Syncing to remote1 ($REMOTE1_HOST)..."
  scp "$ENV_FILE" "$REMOTE1_USER@$REMOTE1_HOST:$REMOTE_DIR/.env"
  rsync -av "$SCRIPT_DIR/generated/"    "$REMOTE1_USER@$REMOTE1_HOST:$REMOTE_DIR/generated/"
  rsync -av "$SCRIPT_DIR/config/"       "$REMOTE1_USER@$REMOTE1_HOST:$REMOTE_DIR/config/"
  rsync -av "$SCRIPT_DIR/config-dummy/" "$REMOTE1_USER@$REMOTE1_HOST:$REMOTE_DIR/config-dummy/"
  echo

  # Sync to remote2 if N > REMOTE1_LIMIT (REMOTE2_HOST validated above)
  if (( N_ROBOTS_VAL > REMOTE1_LIMIT )); then
    echo "📤 Syncing to remote2 ($REMOTE2_HOST)..."
    scp "$ENV_FILE" "$REMOTE2_USER@$REMOTE2_HOST:$REMOTE_DIR/.env"
    rsync -av "$SCRIPT_DIR/generated/"    "$REMOTE2_USER@$REMOTE2_HOST:$REMOTE_DIR/generated/"
    rsync -av "$SCRIPT_DIR/config-dummy/" "$REMOTE2_USER@$REMOTE2_HOST:$REMOTE_DIR/config-dummy/"
    echo
  fi

  # Start SECaaS on d-mutra
  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.secaas.yml" up -d

  if wait_for_secaas; then
    echo "🔄 Synchronizing agent signatures to PostgreSQL..."
    curl -X POST "$SECAAS_URL/sync-agents?n_robots=$N_ROBOTS_VAL" -H "Content-Type: application/json" | jq
    echo "✅ Sync complete."
  else
    exit 1
  fi

  # Start monitoring on d-mutra
  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.monitoring.yml" up -d

  # Start robots on remote1
  echo "🤖 Starting robots on remote1 ($REMOTE1_HOST, robots 1–$(( N_ROBOTS_VAL < REMOTE1_LIMIT ? N_ROBOTS_VAL : REMOTE1_LIMIT )))..."
  if ! remote_compose_up "$REMOTE1_HOST" "$REMOTE1_USER" \
      "$REMOTE_DIR/generated/docker-compose.robots-N${N_ROBOTS_VAL}-remote1.yml"; then
    echo "❌ Failed to start robots on remote1."
    exit 1
  fi

  # Start robots on remote2 if N > REMOTE1_LIMIT
  if (( N_ROBOTS_VAL > REMOTE1_LIMIT )); then
    echo "🤖 Starting robots on remote2 ($REMOTE2_HOST, robots $((REMOTE1_LIMIT+1))–$N_ROBOTS_VAL)..."
    if ! remote_compose_up "$REMOTE2_HOST" "$REMOTE2_USER" \
        "$REMOTE_DIR/generated/docker-compose.robots-N${N_ROBOTS_VAL}-remote2.yml"; then
      echo "❌ Failed to start robots on remote2."
      exit 1
    fi
  fi

  # Start monitoring on remote1
  echo "📊 Starting monitoring on remote1..."
  run_remote "$REMOTE1_HOST" "$REMOTE1_USER" \
    "cd $REMOTE_DIR && COMPOSE_IGNORE_ORPHANS=1 docker compose -p $PROJECT_NAME \
     -f $REMOTE_DIR/docker-compose.monitoring.yml up -d"

  # Start monitoring on remote2 if applicable
  if (( N_ROBOTS_VAL > REMOTE1_LIMIT )); then
    echo "📊 Starting monitoring on remote2..."
    run_remote "$REMOTE2_HOST" "$REMOTE2_USER" \
      "cd $REMOTE_DIR && COMPOSE_IGNORE_ORPHANS=1 docker compose -p $PROJECT_NAME \
       -f $REMOTE_DIR/docker-compose.monitoring.yml up -d"
  fi

  # Start sidecars on remote1
  echo "🛡️  Starting sidecars on remote1..."
  if ! remote_compose_up "$REMOTE1_HOST" "$REMOTE1_USER" \
      "$REMOTE_DIR/generated/docker-compose.attestation-N${N_ROBOTS_VAL}-remote1.yml"; then
    echo "❌ Failed to start sidecars on remote1."
    exit 1
  fi

  # Start sidecars on remote2 if applicable
  if (( N_ROBOTS_VAL > REMOTE1_LIMIT )); then
    echo "🛡️  Starting sidecars on remote2..."
    if ! remote_compose_up "$REMOTE2_HOST" "$REMOTE2_USER" \
        "$REMOTE_DIR/generated/docker-compose.attestation-N${N_ROBOTS_VAL}-remote2.yml"; then
      echo "❌ Failed to start sidecars on remote2."
      exit 1
    fi
  fi

else
# ── LOCAL MODE: everything runs on d-mutra ────────────────────────────────────

  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.robots.yml" up -d

  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.secaas.yml" up -d

  if wait_for_secaas; then
    echo "🔄 Synchronizing agent signatures to PostgreSQL..."
    curl -X POST "$SECAAS_URL/sync-agents?n_robots=$N_ROBOTS_VAL" -H "Content-Type: application/json" | jq
    echo "✅ Sync complete."
  else
    exit 1
  fi

  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.monitoring.yml" up -d

  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.attestation.yml" up -d

fi
# ══════════════════════════════════════════════════════════════════════════════

# ── Reference measurements bootstrap ──────────────────────────────────────────
# Capture the *real* triplet (robot_hash, sidecar_hash, combined_hash) from
# robot1's sidecar, patch every ref-measurements/robot{i}.json with it, and
# push the result back into the SECaaS PostgreSQL via /sync-agents. Then
# reset the on-chain attestation chain so any stale state (from a prior run
# or from the placeholder /sync-agents called above) is cleared.
#
# After this, if the user passed --auto, we trigger /start on SECaaS and on
# every sidecar to launch the workers that we deliberately held back during
# container boot (see the EFFECTIVE_AUTO_START stashing above).
if [[ "$BOOTSTRAP_REFS" == "TRUE" && "$DEPLOY_MODE" == "local" ]]; then
  echo
  echo "🔧 Bootstrapping real reference measurements..."

  # Wait for robot1's sidecar to be reachable AND for its target binary
  # (robot_state_publisher / dummy_publisher) to be alive in the robot
  # container — that's when /digest returns combined_hash instead of an
  # error. Gazebo takes ~30s to spin up the publisher.
  max_attempts=70   # = up to 2 minutes
  attempt=1
  while (( attempt <= max_attempts )); do
    if curl -s "http://localhost:8001/digest" 2>/dev/null | grep -q '"combined_hash"'; then
      echo "✅ robot1 /digest is ready."
      break
    fi
    echo "   (Attempt $attempt/$max_attempts) Waiting for /digest..."
    sleep 2
    (( attempt++ ))
  done
  if (( attempt > max_attempts )); then
    echo "⚠️  /digest didn't come up in time. Skipping bootstrap — attestations will FAIL on placeholder refs until you run bootstrap_ref_measurements.py manually."
  else
    python3 "$SCRIPT_DIR/bootstrap_ref_measurements.py" --robots "$N_ROBOTS_VAL" --sync-secaas \
      && echo "🧹 Resetting on-chain attestation chain..." \
      && curl -s -X POST "$SECAAS_URL/reset" | jq . \
      || echo "⚠️  Bootstrap failed — attestations may close as FAILURE."
  fi

  # Restore the user's AUTO_START intent in .env for human readability /
  # subsequent ./start.sh invocations. Containers already booted with the
  # effective value so this is purely cosmetic for the .env file itself.
  upsert_env "AUTO_START" "$WANT_AUTO_START"

  if [[ "$WANT_AUTO_START" == "TRUE" ]]; then
    echo "▶ Auto-starting attestation workers (SECaaS + sidecars)..."
    curl -s -X POST "$SECAAS_URL/start" | jq .
    for i in $(seq 1 "$N_ROBOTS_VAL"); do
      curl -s -X POST "http://localhost:$((8000 + i))/start" | jq -c .
    done
  fi
elif [[ "$BOOTSTRAP_REFS" == "TRUE" && "$DEPLOY_MODE" == "remote" ]]; then
  echo
  echo "🔧 Bootstrapping real reference measurements (remote)..."

  max_attempts=60   # = up to 2 minutes
  attempt=1
  while (( attempt <= max_attempts )); do
    if curl -s "http://$REMOTE1_HOST:8001/digest" 2>/dev/null | grep -q '"combined_hash"'; then
      echo "✅ robot1 /digest is ready."
      break
    fi
    echo "   (Attempt $attempt/$max_attempts) Waiting for /digest on $REMOTE1_HOST:8001..."
    sleep 2
    (( attempt++ ))
  done
  if (( attempt > max_attempts )); then
    echo "⚠️  /digest didn't come up in time. Run bootstrap manually:"
    echo "      python3 bootstrap_ref_measurements.py --robots $N_ROBOTS_VAL --sidecar-host $REMOTE1_HOST --sync-secaas"
  else
    python3 "$SCRIPT_DIR/bootstrap_ref_measurements.py" \
      --robots "$N_ROBOTS_VAL" \
      --sidecar-host "$REMOTE1_HOST" \
      --sync-secaas \
      && echo "🧹 Resetting on-chain attestation chain..." \
      && curl -s -X POST "$SECAAS_URL/reset" | jq . \
      || echo "⚠️  Bootstrap failed — attestations may close as FAILURE."
  fi
fi

# ── Mission orchestrator ──────────────────────────────────────────────────────
# Local Gazebo + Nav2 only. Started last so the robots (and their mission_agent
# nodes) are already up; the orchestrator waits READY_TIMEOUT_S for each agent's
# mission_status before assigning the mission, then watches the chain for
# attestation FAILUREs and (if MITIGATION=true) reassigns the victim's waypoints.
if [[ "$MISSION_VAL" == "TRUE" && "$DEPLOY_MODE" == "local" ]]; then
  echo
  echo "🧭 Starting mission orchestrator (mitigation=$MITIGATION_VAL, run-tag=$RUN_TAG_VAL)..."
  mkdir -p "$SCRIPT_DIR/experiments/data/mission"
  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.orchestrator.yml" up -d
  echo "   Logs will appear in experiments/data/mission/mission-${RUN_TAG_VAL}.csv"
fi

# ── Formation orchestrator ────────────────────────────────────────────────────
# Local Gazebo only. Started last so the robots (and their formation_agent nodes)
# are already up; it publishes the shared goal, logs Formation Error / TTE over
# time, and watches the chain for attestation FAILUREs to isolate (and, if
# MITIGATION=true, reconfigure away from) the compromised robot.
if [[ "$FORMATION_VAL" == "TRUE" && "$DEPLOY_MODE" == "local" ]]; then
  echo
  echo "🛰️  Starting formation orchestrator (mitigation=$MITIGATION_VAL, scale=$FORMATION_SCALE_VAL, run-tag=$RUN_TAG_VAL)..."
  FDATA_SUBDIR="${FORMATION_DATA_SUBDIR:-formation}"
  mkdir -p "$SCRIPT_DIR/experiments/data/$FDATA_SUBDIR"
  mkdir -p "$SCRIPT_DIR/experiments/data/$FDATA_SUBDIR/bags"
  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.formation.yml" up -d
  echo "   Logs will appear in experiments/data/$FDATA_SUBDIR/formation-${RUN_TAG_VAL}.csv"
fi

echo "🎉 All done."
