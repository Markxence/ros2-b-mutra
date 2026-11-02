#!/usr/bin/env python3
"""
Tamper detection latency benchmark.

Measures wall-clock time between in-memory binary tampering (via
tools/tamper.sh) and the first ❌ FAILURE attestation result logged
by the prover sidecar.

For each trial (worst-case anchoring):
  1. Confirm the loop is healthy after recovery (one ✅ SUCCESS), then wait for
     the prover to log a fresh measurement ("Final digest") — the start of a
     new attestation cycle, just before its inter-cycle (SSP) sleep.
  2. Inject the tamper IMMEDIATELY and capture T0 (host clock). The just-taken
     measurement was clean, so the tamper is first sampled by the NEXT cycle
     one full period (SSP) later. Anchoring to a measurement — not to the
     asynchronous SUCCESS log line, which lags its measurement by the
     confirmation time B — yields the deterministic worst case.
  3. Tail the prover sidecar's log; record T1 from the first
     "[Prover-...] Result: ❌ FAILURE" line with timestamp > T0.
  4. Recover the container(s) for the next trial.

Latency = T1 − T0 ≈ SSP + B, where B (~5–6 s) is the on-chain confirmation
time of the 3-tx peer verification path. The clean worst case is monotonic in
SSP, with intercept B (the irreducible blockchain cost). The old
SUCCESS-anchored scheme instead produced L = ⌈B/P⌉·P (P = the loop period),
a sawtooth that was non-monotonic and bimodal near SSP ≈ B.

Contract support (LV, RR, V2, V3):
  The script reads the deployed contract from the CONTRACT line that start.sh
  writes to .env, and adapts the warmup accordingly.

  - LV (root-of-trust): currentVerifier is the last successfully verified agent.
    Right after robot1 succeeds, currentVerifier == robot1, so robot1 cannot
    verify itself and the contract falls back to SECaaS for the next cycle —
    the *detection* cycle. That is the 2-tx atomic path (~1 block faster than
    the 3-tx peer path), and which path runs is a race against the other robots'
    closings. This is the dominant variance source in LV tamper-detection data.

  - RR (round-robin): ElectVerifier always excludes the prover and picks a peer
    with lastSuccess != 0; it only falls back to SECaaS when no such peer exists.
    After warmup seeds all robots, robot1 is ALWAYS verified by a peer (3-tx),
    so verifier identity is deterministic and the 2-tx/3-tx race disappears.
    Recommended for clean detection-latency micro-benchmarks.

  - V2: LV with the reference anchored on chain, so a peer closes in 2 tx.
    Only its admins can reset it: the warmup calls reset_chain.sh.

  - V3 (jury): a drawn jury closes the cycle, the SECaaS only when no jury can
    be drawn or the jury times out. Reset through the admins, like V2.

Prerequisites:
  - Stack started WITHOUT --auto. This script owns the sidecar lifecycle:
    it runs warmup_fleet + push_ssp before EVERY trial (not just once per
    block) so that each trial starts from the same on-chain state.
      * LV, V2: chain reset + one-shot warmup seeds currentVerifier to a real robot.
      * V3: chain reset + one-shot warmup makes every robot eligible as a juror.
      * RR: ResetChain sets secaasOnly=true; the one-shot warmup seeds every
        robot's lastSuccess via the SECaaS-only path, then the script flips
        /secaas-only=false so trials use peer (round-robin) verification.
  - ITERQ_THRESHOLD=1 in .env (or start.sh --iterq 1).
  - Contract deployed via start.sh --contract lv|rr|v2|v3 (read from .env here).
  - WAIT_FOR_VERIFICATION_RESULT=TRUE (default) so the prover sidecar
    logs the verifier's verdict; otherwise we can't read T1 from its log.
  - Attestation-sidecar image rebuilt with millisecond log timestamps
    (logger.go format "2006-01-02 15:04:05.000").

Usage:
  python3 run_experiments_and_collect_tamper_detection.py --ssp 20000 --trials 10
  python3 run_experiments_and_collect_tamper_detection.py --ssp 1000,5000,10000,20000 --trials 15
  python3 run_experiments_and_collect_tamper_detection.py --ssp 20000 --trials 10 --target sidecar
  python3 run_experiments_and_collect_tamper_detection.py --ssp 20000 --trials 10 --target both
  python3 run_experiments_and_collect_tamper_detection.py --ssp 5000 --trials 30   # stack on RR

Output:
  experiments/data/tamper-detection/results/<target>/SSP{N}ms-batch{B}.csv
  Columns: trial, t0_iso, t1_iso, latency_s, detected, ssp_ms, n_robots, target,
           robot, contract, verifier, verifier_addr, tx_path
  where tx_path ∈ {2tx-secaas, 3tx-peer (LV, RR), 2tx-peer (V2), jury, secaas (V3),
  unknown} is derived from the on-chain
  verifier recorded in the prover's "Verified by:" log line — making each run
  self-diagnosing for the verifier-identity effect described above.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Paths & endpoints ─────────────────────────────────────────────────────────

REPO_ROOT       = Path(__file__).parent.resolve()
TAMPER_SCRIPT   = REPO_ROOT / "tools" / "tamper.sh"
BASE_RESULTS    = REPO_ROOT / "experiments" / "data" / "tamper-detection" / "results"

ROBOTS_BASE_PORT = 8001   # robot1 = 8001, robot2 = 8002, ...
SECAAS_PORT      = 8000
SECAAS_URL       = "http://localhost:8000"

# Log line timestamp prefix produced by logger.go after the ms patch:
#   "2026-05-27 10:24:12.347 - INFO - ..."
LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\b")

# A FAILURE/SUCCESS line from the prover side specifically (not the verifier's "closed" line).
# Match plain ASCII words only — the ✅/❌ emoji are multi-byte UTF-8 that may be
# corrupted when docker logs are piped through a non-UTF-8 locale or cat -v.
PROVER_FAILURE_RE  = re.compile(r"\[Prover-.*?\].*FAILURE")
PROVER_SUCCESS_RE  = re.compile(r"\[Prover-.*?\].*SUCCESS")

# Capture the attestation id from a FAILURE line so we can pair it with the
# adjacent "Verified by: 0x..." line (prover.go logs Result then Verified-by
# back-to-back for the same [Prover-<id>]).
PROVER_FAILURE_ID_RE = re.compile(r"\[Prover-([^\]]+)\].*FAILURE")

# Marks the START of a fresh attestation cycle: the prover logs the folded
# measurement ("Final digest" for K=1, "Rolling-hash digest" for K>1) right
# after sampling the binary and just before the inter-cycle (SSP) sleep.
# Injecting immediately after this line anchors T0 to a clean measurement, so
# the tamper is first sampled one full period later → deterministic worst case.
PROVER_MEASURE_RE = re.compile(r"\[Prover\].*(?:Final digest|Rolling-hash digest)")

# CONTRACT is read from .env in main(); module-level so warmup_fleet / run_trial
# can branch without threading it through everywhere.
CONTRACT = "lv"
CONTRACTS = {"AttestationManagerLV": "lv", "AttestationManagerRR": "rr",
             "AttestationManagerV2": "v2", "AttestationManagerV3": "v3"}

# V2 and V3: only their admins may reset them (the keys start.sh anchors the references with).
ADMIN_KEYS = [REPO_ROOT / "blockchain/quorum-test-network/config/nodes" / v / "accountPrivateKey"
              for v in ("validator2", "validator3")]

# Map lowercase eth address → label ("secaas", "robot1", …), loaded from config/
# at startup. Used to classify the on-chain verifier of each detected FAILURE.
VERIFIER_LABELS: dict[str, str] = {}

# ── Small utilities ───────────────────────────────────────────────────────────

def iso_ms(ts: float) -> str:
    """UTC ISO-8601 with millisecond precision."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_log_ts(line: str) -> float | None:
    """Extract the leading 'YYYY-MM-DD HH:MM:SS.mmm' from a sidecar log line.

    Docker containers default to UTC regardless of the host timezone, so
    logger.go's time.Now() produces UTC timestamps. We must interpret the
    parsed datetime as UTC (not local time) before converting to epoch —
    otherwise the comparison against time.time() (which is always UTC epoch)
    is off by the host's UTC offset.
    Returns a unix epoch float, or None if no match.
    """
    m = LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def docker_logs_since(container: str, since_unix: float) -> str:
    """Fetch docker logs since the given unix timestamp (truncated to second)."""
    # `docker logs --since` accepts unix seconds. Subtract 1s for safety so we
    # don't lose a line whose log second equals T0's second (race condition).
    since = max(0, int(since_unix) - 1)
    try:
        out = subprocess.run(
            ["docker", "logs", "--since", str(since), container],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",  # safe decode regardless of locale
            timeout=10,
        )
        return out.stdout + out.stderr   # sidecar logs to stdout, but be safe
    except subprocess.TimeoutExpired:
        return ""


def container_running(name: str) -> bool:
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.splitlines()
        return name in out
    except subprocess.SubprocessError:
        return False


# ── Contract detection & verifier-identity classification ──────────────────────

def detect_contract_variant() -> str:
    """'lv', 'rr', 'v2' or 'v3', from the CONTRACT line start.sh writes to .env."""
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k.strip() == "CONTRACT":
            if v.strip() not in CONTRACTS:
                sys.exit(f"❌ unknown CONTRACT={v.strip()} in .env")
            return CONTRACTS[v.strip()]
    sys.exit("❌ CONTRACT is missing from .env: run start.sh first")


def reset_chain() -> None:
    """Reset the on-chain attestation chain: through the admins for V2 and V3,
    through the SECaaS otherwise. Raises on failure."""
    if CONTRACT in ("v2", "v3"):
        name = next(k for k, v in CONTRACTS.items() if v == CONTRACT)
        subprocess.run([str(REPO_ROOT / "reset_chain.sh"), name, *map(str, ADMIN_KEYS)],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        requests.post(f"{SECAAS_URL}/reset", timeout=10).raise_for_status()


def load_verifier_labels() -> dict[str, str]:
    """Build {lowercase eth address → label} from config/secaas.json + robot*.json.

    Lets us name the on-chain verifier of each detected FAILURE (secaas vs a
    specific peer) and thereby infer the transaction path (2-tx vs 3-tx).
    """
    labels: dict[str, str] = {}
    cdir = REPO_ROOT / "config"
    sec = cdir / "secaas.json"
    if sec.exists():
        try:
            labels[json.loads(sec.read_text())["eth_address"].lower()] = "secaas"
        except (OSError, ValueError, KeyError):
            pass
    for p in sorted(cdir.glob("robot*.json")):
        try:
            d = json.loads(p.read_text())
            labels[d["eth_address"].lower()] = d["name"]
        except (OSError, ValueError, KeyError):
            pass
    return labels


def classify_verifier(addr: str | None) -> tuple[str, str]:
    """Return (label, tx_path) for an on-chain verifier address.

    secaas  → 2tx-secaas (atomic resolve: SendEvidence + ResolveAttestationSECaaS)
    robotN  → 3tx-peer    (SendEvidence + SendRefSignature + CloseAttestationProcess)
    V2 has the reference on chain, so a peer closes in 2 tx (no SendRefSignature).
    V3 names the juror whose vote closed the cycle; the tx count depends on the
    jury size and on late votes, so the path is just "jury" or "secaas".
    """
    if not addr:
        return "", ""
    label = VERIFIER_LABELS.get(addr.lower(), "unknown")
    if CONTRACT == "v3":
        return label, {"secaas": "secaas"}.get(label, "jury" if label.startswith("robot") else "unknown")
    if label == "secaas":
        return label, "2tx-secaas"
    if label.startswith("robot"):
        return label, "2tx-peer" if CONTRACT == "v2" else "3tx-peer"
    return label, "unknown"


def set_secaas_only(enabled: bool) -> bool:
    """Toggle the RR contract's secaasOnly flag via the SECaaS HTTP API.

    enabled=False switches from startup mode (SECaaS is sole verifier) to mutual
    round-robin peer election. No-op semantics for LV (caller must not invoke).
    """
    try:
        r = requests.post(
            f"{SECAAS_URL}/secaas-only",
            params={"enabled": "true" if enabled else "false"},
            timeout=35,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"   ⚠️  /secaas-only={enabled} failed: {e}", file=sys.stderr)
        return False


# ── HTTP config push ──────────────────────────────────────────────────────────

def stop_attestation(port: int) -> None:
    """POST /stop — idempotent, ignored if sidecar is already idle."""
    try:
        requests.post(f"http://localhost:{port}/stop", timeout=10)
    except requests.RequestException:
        pass


def start_attestation(port: int) -> None:
    """POST /start — begin (or resume) the attestation loop."""
    try:
        requests.post(f"http://localhost:{port}/start", timeout=10)
    except requests.RequestException:
        pass


def push_ssp(port: int, ssp_ms: int) -> None:
    """Set the prover's attestation interval.

    /config returns 409 while appState.measuring == true, so we stop the
    attestation loop first, push the config, then restart it.
    """
    stop_attestation(port)
    time.sleep(0.3)   # brief pause for the measuring flag to clear
    try:
        r = requests.post(
            f"http://localhost:{port}/config",
            json={"attestation_interval_ms": ssp_ms},
            timeout=5,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"   ⚠️  /config push failed: {e}", file=sys.stderr)
    start_attestation(port)


def sidecar_alive(port: int, timeout: float = 30.0) -> bool:
    """Poll /status until the sidecar HTTP server responds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(f"http://localhost:{port}/status", timeout=2)
            return True
        except requests.RequestException:
            time.sleep(0.5)
    return False


def warmup_fleet(n_robots: int, ssp_ms: int) -> bool:
    """Reset chain and run a one-shot warmup cycle (export disabled) so every
    trial starts from identical on-chain state. Returns True on success, False
    if any robot's warmup times out.

      LV, V2: seeds currentVerifier to a real robot (root-of-trust rotation).
      V3: every robot succeeds once, so every robot is an eligible juror.
      RR: ResetChain sets secaasOnly=true, so the one-shot cycle seeds each
          robot's lastSuccess via SECaaS; afterwards we flip secaasOnly=false
          so trials use peer (round-robin) verification — making the verifier
          for robot1's detection cycle a deterministic peer (3-tx path).
    """
    print(f"   🔥 Fleet warmup ({CONTRACT.upper()}): resetting chain and seeding verifier pool…")

    try:
        reset_chain()
    except (requests.RequestException, subprocess.CalledProcessError) as e:
        print(f"   ❌ chain reset failed: {e}", file=sys.stderr)
        return False

    # Start SECaaS event listener — required to process AttestationStarted events
    # and send reference signatures. Without this, robot sidecars time out waiting
    # for ReadyForEvaluation. Idempotent if already running.
    try:
        requests.post(f"{SECAAS_URL}/start", timeout=10).raise_for_status()
    except requests.RequestException as e:
        print(f"   ❌ SECaaS /start failed: {e}", file=sys.stderr)
        return False

    warmup_since = time.time()
    for i in range(1, n_robots + 1):
        port = ROBOTS_BASE_PORT + (i - 1)
        stop_attestation(port)
        time.sleep(0.1)
        try:
            requests.post(f"http://localhost:{port}/config",
                          json={"one_shot": True, "export_enabled": False,
                                "attestation_interval_ms": ssp_ms},
                          timeout=5)
        except requests.RequestException as e:
            print(f"   ⚠️  robot{i} config failed: {e}", file=sys.stderr)
        start_attestation(port)

    # Warmup timeout is blockchain-driven (one-shot has no SSP sleep).
    # With N robots all submitting concurrent attestations, the last robot's
    # close transaction may lag behind robot1's by N×block-period. Use a
    # generous flat 90s so transient Besu delays don't abort the experiment.
    warmup_timeout = 90.0
    print(f"   ⏳ Waiting for {n_robots} robot(s) to complete warmup cycle…")
    for i in range(1, n_robots + 1):
        cname = f"robot{i}-sidecar"
        if wait_for_prover_event(cname, PROVER_SUCCESS_RE, warmup_since, warmup_timeout) is None:
            print(f"   ⚠️  {cname} timed out during warmup")
            return False
        print(f"      ✅ robot{i} warmed up")

    # Restore continuous mode — loops are stopped, push_ssp will restart them
    for i in range(1, n_robots + 1):
        try:
            requests.post(f"http://localhost:{ROBOTS_BASE_PORT + (i - 1)}/config",
                          json={"one_shot": False}, timeout=5)
        except requests.RequestException:
            pass

    # RR: all robots now have lastSuccess != 0. Leave startup (SECaaS-only) mode
    # so that trials elect a peer verifier round-robin (never SECaaS, never the
    # prover itself). LV has no secaasOnly flag — skip the toggle entirely.
    if CONTRACT == "rr":
        if not set_secaas_only(False):
            return False
        print("   ✅ Warmup done. RR peer election enabled (verifier ≠ prover, ≠ SECaaS).\n")
    elif CONTRACT == "v3":
        print("   ✅ Warmup done. Every robot is an eligible juror.\n")
    else:
        print("   ✅ Warmup done. currentVerifier is a real robot.\n")
    return True


def run_warmup(n_robots: int, ssp_ms: int) -> None:
    """warmup_fleet with up to 3 retries; aborts the run on persistent failure.

    A single robot occasionally fails to close its one-shot warmup attestation
    within the timeout (concurrent re-seeding right after a chain reset stresses
    the SECaaS nonce sequence / EventWatcher). A retry — which performs a fresh
    reset — almost always clears it.
    """
    for attempt in range(3):
        if warmup_fleet(n_robots, ssp_ms):
            return
        if attempt == 2:
            sys.exit("❌ Warmup failed after 3 attempts — aborting")
        print(f"   ⚠️  Warmup attempt {attempt + 1} failed, retrying…")
        time.sleep(5)


def wait_for_digest(port: int, timeout: float = 60.0) -> bool:
    """Poll /digest until the sidecar reports a valid combined_hash.

    This confirms the target process (robot_state_publisher / sidecar binary)
    is running and has been located by the sidecar — safe to start attestation.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"http://localhost:{port}/digest", timeout=3)
            if r.status_code == 200 and "combined_hash" in r.json():
                return True
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1.0)
    return False


# ── Wait helpers ──────────────────────────────────────────────────────────────

def wait_for_prover_event(container: str, pattern: re.Pattern, since: float,
                          timeout: float, poll_s: float = 0.1) -> float | None:
    """Block until a log line matching `pattern` appears with parsed timestamp > since.

    Returns the parsed log timestamp (unix float) of the first match, or None on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        logs = docker_logs_since(container, since)
        for line in logs.splitlines():
            ts = parse_log_ts(line)
            if ts is None or ts < since:
                continue
            if pattern.search(line):
                return ts
        time.sleep(poll_s)
    return None


def wait_for_clean_baseline(container: str, since: float, ssp_ms: int,
                            timeout_factor: float = 3.0) -> bool:
    """Wait for ≥1 ✅ SUCCESS attestation logged after `since`.

    Timeout = max(30s, ssp_ms * timeout_factor / 1000). At SSP=20s we wait ≤60s.
    """
    timeout = max(30.0, ssp_ms * timeout_factor / 1000.0)
    return wait_for_prover_event(container, PROVER_SUCCESS_RE, since, timeout) is not None


def wait_for_prover_failure(container: str, since: float, timeout: float,
                            poll_s: float = 0.1) -> tuple[float | None, str | None]:
    """Block until the first FAILURE line logged after `since`.

    Returns (t1, verifier_addr): t1 is the FAILURE log timestamp (unix float),
    verifier_addr is the on-chain verifier from the same attestation's
    "Verified by: 0x…" line (prover.go logs Result then Verified-by back-to-back
    for the same [Prover-<id>]). verifier_addr may be None if that line hasn't
    flushed yet; we re-fetch briefly to catch it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        logs = docker_logs_since(container, since)
        lines = logs.splitlines()
        for idx, line in enumerate(lines):
            ts = parse_log_ts(line)
            if ts is None or ts < since:
                continue
            m = PROVER_FAILURE_ID_RE.search(line)
            if not m:
                continue
            att = m.group(1)
            vaddr = _find_verifier_addr(lines, idx, att)
            # The Verified-by line is logged microseconds after Result but may
            # not be in this batch yet — re-fetch up to ~1s to pair it.
            if vaddr is None:
                pair_deadline = time.time() + 1.0
                while vaddr is None and time.time() < pair_deadline:
                    time.sleep(0.1)
                    relines = docker_logs_since(container, since).splitlines()
                    vaddr = _find_verifier_addr(relines, 0, att)
            return ts, vaddr
        time.sleep(poll_s)
    return None, None


def _find_verifier_addr(lines: list[str], start_idx: int, att_id: str) -> str | None:
    """Scan lines[start_idx:] for '[Prover-<att_id>] … Verified by: 0x…'.
    V3 logs 'Verified by: the jury, closing vote from 0x…' for a jury cycle."""
    vre = re.compile(r"\[Prover-" + re.escape(att_id)
                     + r"\].*Verified by:\s*(?:the jury, closing vote from\s*)?(0x[0-9a-fA-F]+)")
    for l in lines[start_idx:]:
        mm = vre.search(l)
        if mm:
            return mm.group(1)
    return None


# ── Recovery ──────────────────────────────────────────────────────────────────

def recover(robot: str, sidecar: str, target: str) -> None:
    """Restore the container(s) corrupted by the tamper.

    state_publisher tamper → restart the robot container (kills sidecar via
    shared PID namespace), then start the sidecar.
    sidecar tamper         → restart only the sidecar.
    """
    if target == "sidecar":
        subprocess.run(["docker", "restart", sidecar], check=True,
                       stdout=subprocess.DEVNULL)
    else:
        subprocess.run(["docker", "restart", robot], check=True,
                       stdout=subprocess.DEVNULL)
        # robot1-sidecar exits with 137 when its PID namespace dies.
        # Brief sleep ensures Docker has fully processed the kill before we check.
        time.sleep(1)
        if not container_running(sidecar):
            subprocess.run(["docker", "start", sidecar], check=True,
                           stdout=subprocess.DEVNULL)


# ── Tamper invocation ─────────────────────────────────────────────────────────

def inject_tamper(robot: str, target: str) -> None:
    """Run tools/tamper.sh — exits immediately after writing the byte."""
    cmd = [str(TAMPER_SCRIPT), robot]
    if target != "state_publisher":
        cmd += ["--target", target]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


# ── CSV output ────────────────────────────────────────────────────────────────

def next_batch_csv(target: str, ssp_ms: int) -> Path:
    """Pick the next unused SSP{N}ms-batch{B}.csv name under results/<target>/."""
    out_dir = BASE_RESULTS / target
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(rf"^SSP{ssp_ms}ms-batch(\d+)\.csv$")
    existing = [pattern.match(p.name) for p in out_dir.iterdir() if p.is_file()]
    used = [int(m.group(1)) for m in existing if m]
    nxt = (max(used) + 1) if used else 1
    return out_dir / f"SSP{ssp_ms}ms-batch{nxt}.csv"


def append_row(csv_path: Path, row: dict) -> None:
    is_new = not csv_path.exists()
    with csv_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if is_new:
            w.writeheader()
        w.writerow(row)


# ── Trial loop ────────────────────────────────────────────────────────────────

def run_trial(trial_idx: int, robot: str, sidecar: str, sidecar_port: int,
              target: str, ssp_ms: int, n_robots: int, csv_path: Path) -> bool:
    """One injection + detection + recovery cycle. Returns True if measured."""
    print(f"   [{trial_idx}] ", end="", flush=True)

    # 1. Baseline — confirm the loop is healthy after recovery (one SUCCESS),
    #    then wait for the START of a fresh measurement cycle.
    #
    #    Worst-case anchoring: we inject immediately after the prover logs a
    #    fresh measurement ("Final digest"), i.e. right at the beginning of the
    #    inter-cycle (SSP) sleep. The just-taken measurement is clean (sampled
    #    before the tamper), so the tamper is first observed by the NEXT
    #    measurement one full period (SSP) later, and confirmed B seconds after
    #    that. This anchors T0 to a measurement rather than to the asynchronous
    #    SUCCESS log line (which lags its measurement by B), giving the clean
    #    deterministic worst case L ≈ SSP + B and removing the ⌈B/P⌉·P
    #    quantization and the SSP≈B bimodality of the old SUCCESS-anchored scheme.
    timeout_base = max(30.0, ssp_ms * 3.0 / 1000.0)
    print("baseline…", end=" ", flush=True)
    if wait_for_prover_event(sidecar, PROVER_SUCCESS_RE, time.time(), timeout_base) is None:
        # one retry — the loop may still be settling right after recovery
        print("(retry)…", end=" ", flush=True)
        if wait_for_prover_event(sidecar, PROVER_SUCCESS_RE, time.time(), timeout_base) is None:
            print("\n      ⚠️  no SUCCESS observed (loop unhealthy) — skipping trial")
            return False

    # Wait for the next fresh measurement line — start of a new cycle.
    print("sync…", end=" ", flush=True)
    if wait_for_prover_event(sidecar, PROVER_MEASURE_RE, time.time(), timeout_base) is None:
        print("\n      ⚠️  no fresh measurement observed — skipping trial")
        return False

    # 2. Inject immediately (start of the inter-cycle sleep).
    # T0 is recorded right after inject_tamper() returns. The ~200–400 ms exec
    # dispatch is excluded, so T0 is within ~20–50 ms of the actual byte write.
    print("tamper…", end=" ", flush=True)
    try:
        inject_tamper(robot, target)
    except subprocess.CalledProcessError as e:
        print(f"\n      ❌ tamper.sh failed: {e}")
        return False
    t0 = time.time()
    t0_iso = iso_ms(t0)
    print(f"T0={t0_iso}", end=" ", flush=True)

    # 3. Wait for FAILURE in the prover log. Cap wait at 3×SSP + 10s.
    #    Also capture the on-chain verifier so we can record the tx path
    #    (2tx-secaas vs 3tx-peer) that produced this detection.
    timeout = max(30.0, (ssp_ms / 1000.0) * 3.0 + 10.0)
    t1, verifier_addr = wait_for_prover_failure(sidecar, t0, timeout)

    detected = t1 is not None
    latency  = round(t1 - t0, 3) if detected else None
    t1_iso   = iso_ms(t1) if detected else ""
    verifier_label, tx_path = classify_verifier(verifier_addr) if detected else ("", "")

    if detected:
        vinfo = f"  [{tx_path or '?'} via {verifier_label or '?'}]"
        print(f"→ FAILURE@{t1_iso}  Δ={latency:.3f}s{vinfo}")
    else:
        print(f"\n      ❌ no FAILURE detected within {timeout:.0f}s")

    # 4. Persist — always write the row so detection_rate is computable:
    #    detection_rate = non-empty latency_s rows / total rows.
    append_row(csv_path, {
        "trial":         trial_idx,
        "t0_iso":        t0_iso,
        "t1_iso":        t1_iso,
        "latency_s":     f"{latency:.3f}" if detected else "",
        "detected":      int(detected),
        "ssp_ms":        ssp_ms,
        "n_robots":      n_robots,
        "target":        target,
        "robot":         robot,
        "contract":      CONTRACT,
        "verifier":      verifier_label,
        "verifier_addr": verifier_addr or "",
        "tx_path":       tx_path,
    })

    if not detected:
        recover(robot, sidecar, target)
        sidecar_alive(sidecar_port)
        if not wait_for_digest(sidecar_port):
            print("\n      ⚠️  /digest never returned combined_hash after recovery")
        return False

    # 5. Recover for the next trial.
    print("      🔄 recovering…", end=" ", flush=True)
    try:
        recover(robot, sidecar, target)
    except subprocess.CalledProcessError as e:
        print(f"\n      ❌ recovery failed: {e}")
        return False
    if not sidecar_alive(sidecar_port, timeout=45):
        print("\n      ❌ sidecar did not come back online")
        return False
    print("ok")

    # 6. Wait until the sidecar has located the measurement target (critical
    #    after robot-container restart where robot_state_publisher gets a new
    #    PID). The outer loop then calls warmup_fleet + push_ssp to reset the
    #    chain and seed currentVerifier to identical state for every trial.
    if not wait_for_digest(sidecar_port):
        print("\n      ⚠️  /digest never returned combined_hash after recovery")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_ssps(s: str) -> list[int]:
    out = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid SSP value: {chunk!r}")
    if not out:
        raise argparse.ArgumentTypeError("--ssp requires at least one value")
    return out


def main():
    p = argparse.ArgumentParser(
        description="Tamper detection latency benchmark (B-MuTRA).",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--ssp", type=parse_ssps, required=True,
                   help="Attestation interval(s) in ms, comma-separated. e.g. 1000,5000,10000,20000")
    p.add_argument("--trials", type=int, default=10,
                   help="Number of injection trials per (target, SSP). Default: 10")
    p.add_argument("--target", choices=["state_publisher", "sidecar", "both"],
                   default="state_publisher",
                   help="Tamper target. 'both' runs state_publisher then sidecar.")
    p.add_argument("--robot", default="robot1",
                   help="Which robot to target. Default: robot1")
    p.add_argument("--robots", type=int, default=4,
                   help="Total fleet size. All robot sidecars are started and must "
                        "each pass a baseline attestation cycle (so they are "
                        "registered in the on-chain verifier pool) before trials "
                        "begin. Default: 4")
    args = p.parse_args()

    # Resolve the contract variant and load address→label map for verifier
    # classification. These populate the module globals used by warmup_fleet /
    # run_trial so the rest of the code stays contract-agnostic.
    global CONTRACT, VERIFIER_LABELS
    CONTRACT = detect_contract_variant()
    VERIFIER_LABELS = load_verifier_labels()
    if not VERIFIER_LABELS:
        print("⚠️  no config/*.json addresses loaded — verifier/tx_path columns "
              "will be 'unknown'.", file=sys.stderr)

    if not TAMPER_SCRIPT.exists():
        sys.exit(f"❌ tamper script not found: {TAMPER_SCRIPT}")

    sidecar = f"{args.robot}-sidecar"
    # Port mapping: robot1 → 8001, robot2 → 8002, ...
    try:
        idx = int(args.robot.removeprefix("robot"))
    except ValueError:
        sys.exit(f"❌ cannot infer port for {args.robot} (expected robot<N>)")
    sidecar_port = ROBOTS_BASE_PORT + (idx - 1)

    if not container_running(sidecar):
        sys.exit(f"❌ container {sidecar} is not running — start the stack first")
    if not sidecar_alive(sidecar_port, timeout=10):
        sys.exit(f"❌ sidecar {sidecar} not reachable on port {sidecar_port}")

    targets = ["state_publisher", "sidecar"] if args.target == "both" else [args.target]

    if args.robots < idx:
        sys.exit(f"❌ --robots {args.robots} is less than target robot index {idx}")

    print(f"📋 robot={args.robot}  sidecar={sidecar}:{sidecar_port}  contract={CONTRACT.upper()}")
    print(f"📋 fleet={args.robots} robot(s)  targets={targets}  ssps={args.ssp}ms  trials={args.trials} each")
    if CONTRACT == "rr":
        print("📋 RR: trials use round-robin peer verification (deterministic 3-tx path).")
    elif CONTRACT == "v3":
        print("📋 V3: the jury or the SECaaS closes the cycle — tx_path column records which.")
    elif CONTRACT == "v2":
        print("📋 V2: verifier may be SECaaS or a peer, both 2-tx — tx_path column records which.")
    else:
        print("📋 LV: verifier may be SECaaS (2-tx) or a peer (3-tx) — tx_path column records which.")
    print(f"📁 results → {BASE_RESULTS}/<target>/SSPNms-batchN.csv")

    total_attempted = 0
    total_succeeded = 0

    for target in targets:
        for ssp_ms in args.ssp:
            csv_path = next_batch_csv(target, ssp_ms)
            print(f"\n{'='*60}")
            print(f"🏁 target={target}  SSP={ssp_ms}ms")
            print(f"   output → {csv_path.relative_to(REPO_ROOT)}")
            print(f"{'='*60}")

            # Verify all fleet sidecars are reachable before starting.
            for i in range(1, args.robots + 1):
                port = ROBOTS_BASE_PORT + (i - 1)
                if not sidecar_alive(port, timeout=10):
                    sys.exit(f"❌ robot{i}-sidecar not reachable on port {port} — start the stack first")

            # RR seeds lastSuccess once and that state persists across trials
            # (container recovery does not clear the chain), so we warm up a
            # SINGLE time before the trial loop. Re-resetting every trial only
            # re-introduces the fragile concurrent re-seeding (occasional 90s
            # warmup timeouts) for no determinism benefit — the verifier is a
            # peer (3-tx) on every cycle regardless of which peer.
            #
            # LV must reset before EVERY trial: currentVerifier evolves during
            # recovery and determines the 2-tx vs 3-tx path, so identical
            # on-chain state per trial is required to control that variance.
            # V2 rotates the same way. V3 too: the detected failure costs
            # robot1 its eligibility, and absent or dissenting jurors lose theirs.
            if CONTRACT == "rr":
                run_warmup(args.robots, ssp_ms)

            for i in range(1, args.trials + 1):
                if CONTRACT != "rr":
                    run_warmup(args.robots, ssp_ms)
                for j in range(1, args.robots + 1):
                    push_ssp(ROBOTS_BASE_PORT + (j - 1), ssp_ms)

                total_attempted += 1
                if run_trial(i, args.robot, sidecar, sidecar_port,
                             target, ssp_ms, args.robots, csv_path):
                    total_succeeded += 1

    print(f"\n🎉 Done. {total_succeeded}/{total_attempted} trials recorded.")
    print(f"   Results in {BASE_RESULTS}")


if __name__ == "__main__":
    main()
