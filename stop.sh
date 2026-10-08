#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# One Compose project name for ALL stacks
PROJECT_NAME="ros2-b-mutra"

# Remote hosts: same variables and defaults as start.sh
REMOTE1_HOST="${REMOTE1_HOST:-10.5.1.20}"
REMOTE1_USER="${REMOTE1_USER:-nextnet}"
REMOTE1_LIMIT=64
REMOTE2_HOST="${REMOTE2_HOST:-}"   # set when a second remote host is available
REMOTE2_USER="${REMOTE2_USER:-nextnet}"
REMOTE_DIR="~/ros2-b-mutra"

# Read N_ROBOTS and DEPLOY_MODE from .env
ENV_FILE="$SCRIPT_DIR/.env"
N_ROBOTS_VAL=4
DEPLOY_MODE="local"

if [[ -f "$ENV_FILE" ]]; then
  _n=$(grep -oP '(?<=^N_ROBOTS=)\d+' "$ENV_FILE" || true)
  [[ -n "$_n" ]] && N_ROBOTS_VAL="$_n"
  _m=$(grep -oP '(?<=^DEPLOY_MODE=)\w+' "$ENV_FILE" || true)
  [[ -n "$_m" ]] && DEPLOY_MODE="$_m"
fi

# ── Stop monitoring (d-mutra always) ──────────────────────────────────────────
docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.monitoring.yml" down

# ══════════════════════════════════════════════════════════════════════════════
if [[ "$DEPLOY_MODE" == "remote" ]]; then
# ── REMOTE MODE teardown ──────────────────────────────────────────────────────

  echo "🌐 Remote mode teardown (N=$N_ROBOTS_VAL)..."

  # Stop monitoring on remote1
  ssh "$REMOTE1_USER@$REMOTE1_HOST" \
    "cd $REMOTE_DIR && docker compose -p $PROJECT_NAME \
     -f $REMOTE_DIR/docker-compose.monitoring.yml down" || true

  # Stop monitoring on remote2 if used
  if (( N_ROBOTS_VAL > REMOTE1_LIMIT )) && [[ -n "$REMOTE2_HOST" ]]; then
    ssh "$REMOTE2_USER@$REMOTE2_HOST" \
      "cd $REMOTE_DIR && docker compose -p $PROJECT_NAME \
       -f $REMOTE_DIR/docker-compose.monitoring.yml down" || true
  fi

  # Stop sidecars and robots on remote1
  ssh "$REMOTE1_USER@$REMOTE1_HOST" \
    "cd $REMOTE_DIR && \
     docker compose -p $PROJECT_NAME --project-directory $REMOTE_DIR \
       -f $REMOTE_DIR/generated/docker-compose.attestation-N${N_ROBOTS_VAL}-remote1.yml down && \
     docker compose -p $PROJECT_NAME --project-directory $REMOTE_DIR \
       -f $REMOTE_DIR/generated/docker-compose.robots-N${N_ROBOTS_VAL}-remote1.yml down"

  # Stop sidecars and robots on remote2 if used
  if (( N_ROBOTS_VAL > REMOTE1_LIMIT )) && [[ -n "$REMOTE2_HOST" ]]; then
    ssh "$REMOTE2_USER@$REMOTE2_HOST" \
      "cd $REMOTE_DIR && \
       docker compose -p $PROJECT_NAME --project-directory $REMOTE_DIR \
         -f $REMOTE_DIR/generated/docker-compose.attestation-N${N_ROBOTS_VAL}-remote2.yml down && \
       docker compose -p $PROJECT_NAME --project-directory $REMOTE_DIR \
         -f $REMOTE_DIR/generated/docker-compose.robots-N${N_ROBOTS_VAL}-remote2.yml down"
  fi

else
# ── LOCAL MODE teardown: everything on d-mutra ────────────────────────────────

  # Orchestrator (patrol mission experiments) — harmless if it was never started.
  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.orchestrator.yml" down 2>/dev/null || true
  # Formation experiment (orchestrator + bag-recorder) — stopping the recorder
  # here is what flushes/closes the ros2 bag cleanly. Harmless if never started.
  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.formation.yml" down 2>/dev/null || true
  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.attestation.yml" down
  docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.robots.yml" down

fi
# ══════════════════════════════════════════════════════════════════════════════

# SECaaS and blockchain always on d-mutra
docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.secaas.yml" down

# Remove blockchain network
cd "$SCRIPT_DIR/blockchain/quorum-test-network"
./remove.sh

# Reset EventWatcher checkpoints (seen_keys/from_block)
CHECKPOINT_DIR="$SCRIPT_DIR/checkpoints"
RESET_SCRIPT="$CHECKPOINT_DIR/reset_event_watcher.sh"

if [[ -d "$CHECKPOINT_DIR" && -f "$RESET_SCRIPT" ]]; then
  echo "🧹 Resetting EventWatcher checkpoints..."
  docker run --rm -v "$CHECKPOINT_DIR":/cp alpine sh -c 'find /cp -name "*.json" -delete'
else
  echo "⚠️  Checkpoints reset skipped (missing $RESET_SCRIPT or $CHECKPOINT_DIR)"
fi
