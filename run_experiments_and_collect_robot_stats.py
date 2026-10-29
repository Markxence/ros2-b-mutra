#!/usr/bin/env python3
"""
Performance benchmark orchestrator.

Measures ROS2 topic jitter with and without attestation sidecars to quantify
the performance impact of continuous memory-read attestation on the robot process.

Sidecars must be started with WAIT_FOR_VERIFICATION_RESULT=FALSE so that each
attestation cycle is hash-compute + tx-send only (fire-and-forget), isolating
the memory-read/CPU interference from blockchain confirmation latency.

Usage:
  python3 run_robot_benchmark.py --condition no_sidecar  --topic tf --duration 300
  python3 run_robot_benchmark.py --condition with_sidecar --topic tf --duration 300 --ssp 5000 --cpu-limit 0.4

Prerequisites:
  WAIT_FOR_VERIFICATION_RESULT=FALSE in .env
  docker compose -f docker-compose.robots.yml -f docker-compose.benchmark-collector.yml up -d
  (add docker-compose.attestation.yml for with_sidecar condition)
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

COLLECTOR_URL    = "http://localhost:7001"
SECAAS_PORT      = 8000
ROBOTS_BASE_PORT = 8001
COLLECTOR_ROBOT  = 1   # single robot whose topic is recorded and attested
DEFAULT_DURATION_S = 300

BASE_RESULTS_DIR      = Path(__file__).parent / "experiments/data/robot-stats/results"
COLLECTOR_RESULTS_DIR = "/experiments/data/robot-stats/results"   # container-side path

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_env_defaults():
    ssp_ms    = 20000
    iterq     = 1
    cpu_limit = None   # None = uncapped (no deploy.resources.limits in compose)
    n_robots  = 4
    try:
        with open(os.path.join(_SCRIPT_DIR, ".env")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if key == "ATTESTATION_INTERVAL_MS":
                    try: ssp_ms = int(val)
                    except ValueError: pass
                elif key == "CPU_LIMIT":
                    if val.lower() == "none" or val == "":
                        cpu_limit = None
                    else:
                        try: cpu_limit = float(val)
                        except ValueError: pass
                elif key == "ITERQ_THRESHOLD":
                    try: iterq = int(val)
                    except ValueError: pass
                elif key == "N_ROBOTS":
                    try: n_robots = int(val)
                    except ValueError: pass
    except FileNotFoundError:
        pass
    return ssp_ms, iterq, cpu_limit, n_robots


_DEFAULT_SSP, _DEFAULT_ITERQ, _DEFAULT_CPU_LIMIT, _DEFAULT_N_ROBOTS = _read_env_defaults()


def _build_run_tag(ssp_ms, iterq, cpu_limit):
    cpu_str = "NC" if cpu_limit is None else f"{cpu_limit:.2f}".replace(".", "p")
    return f"SSP{ssp_ms}ms-ITERQ{iterq}-cpu{cpu_str}"


# ── Sidecar helpers (mirrors run_experiments_and_collect_results.py) ───────────

def build_sidecars(n_robots: int):
    sidecars = {"secaas": SECAAS_PORT}
    for i in range(1, n_robots + 1):
        sidecars[f"robot{i}"] = ROBOTS_BASE_PORT + i - 1
    return sidecars


def wait_for_http(url, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            requests.get(url, timeout=2)
            return True
        except requests.RequestException:
            time.sleep(1)
    return False


def trigger_action(name, port, action, timeout=5):
    try:
        resp = requests.post(f"http://localhost:{port}/{action}", timeout=timeout)
        resp.raise_for_status()
        return f"✔ {name}: {action}"
    except Exception as e:
        return f"❌ {name}: {e}"


def get_status(name, port):
    try:
        return requests.get(f"http://localhost:{port}/status", timeout=5).json().get("status", "unknown")
    except Exception:
        return "error"


def _poll_parallel(robot_sidecars):
    with ThreadPoolExecutor(max_workers=len(robot_sidecars)) as ex:
        futs = {ex.submit(get_status, n, p): n for n, p in robot_sidecars.items()}
        return {futs[f]: f.result() for f in as_completed(futs)}


def wait_for_all_running(robot_sidecars, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        if all(s in ("running", "finished") for s in _poll_parallel(robot_sidecars).values()):
            return True
        time.sleep(0.5)
    return False


def wait_for_all_finished(robot_sidecars, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        if all(s == "finished" for s in _poll_parallel(robot_sidecars).values()):
            return True
        time.sleep(2)
    return False


def set_config(name, port, payload):
    try:
        requests.post(f"http://localhost:{port}/config", json=payload, timeout=5).raise_for_status()
    except Exception:
        pass


def set_config_all(robot_sidecars, payload):
    with ThreadPoolExecutor(max_workers=len(robot_sidecars)) as ex:
        list(as_completed([ex.submit(set_config, n, p, payload) for n, p in robot_sidecars.items()]))


def stop_all(sidecars):
    with ThreadPoolExecutor(max_workers=len(sidecars)) as ex:
        futs = [ex.submit(trigger_action, n, p, "stop", 15) for n, p in sidecars.items()]
        for f in as_completed(futs):
            print(f"      {f.result()}")




# ── Collector helpers ──────────────────────────────────────────────────────────

def wait_for_topics_active(timeout=60):
    """Block until collector reports at least 1 message from the measured robot."""
    print(f"   ⏳ Waiting for robot{COLLECTOR_ROBOT} to publish...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            n = requests.get(f"{COLLECTOR_URL}/status", timeout=3).json().get("messages_received", 0)
            if n >= 1:
                print(f"   ✅ Topic active ({n} msgs received).")
                return True
        except Exception:
            pass
        time.sleep(1)
    print("   ⚠️ Timeout waiting for topic.")
    return False


# ── Run loop ───────────────────────────────────────────────────────────────────

def run_benchmark_loop(run_id, condition, folder, topic_slug, sidecars, robot_sidecars, run_tag=None, duration_s=DEFAULT_DURATION_S, ssp_ms=None):
    print(f"\n{'='*55}\n🏁 Run {run_id}  [{condition} / {topic_slug}]\n{'='*55}")

    fname = f"{run_tag}-run{run_id}.csv" if run_tag else f"run{run_id}.csv"
    host_output      = BASE_RESULTS_DIR / topic_slug / condition / folder / fname
    container_output = f"{COLLECTOR_RESULTS_DIR}/{topic_slug}/{condition}/{folder}/{fname}"
    host_output.parent.mkdir(parents=True, exist_ok=True)

    # Reset collector state from previous run
    requests.post(f"{COLLECTOR_URL}/reset", timeout=5)

    # Wait until robots are publishing before starting the measurement window
    wait_for_topics_active(timeout=60)

    if condition == "with_sidecar":
        cfg = {"export_enabled": False}
        if ssp_ms is not None:
            cfg["attestation_interval_ms"] = ssp_ms
        set_config_all(robot_sidecars, cfg)
        set_config("secaas", SECAAS_PORT, {"export_enabled": False})

        print("   [Agents] Starting...")
        with ThreadPoolExecutor(max_workers=len(sidecars)) as ex:
            for f in as_completed([ex.submit(trigger_action, n, p, "start") for n, p in sidecars.items()]):
                print(f"      {f.result()}")
        wait_for_all_running(robot_sidecars)

    # Start collector only after sidecars are up (or immediately for baseline)
    print(f"   [Collector] Recording → {host_output}")
    requests.post(f"{COLLECTOR_URL}/start", json={
        "output_file": container_output,
        "duration_s":  duration_s + 10,    # buffer; we stop explicitly below
    }, timeout=5)

    print(f"   ⏳ {'Running' if condition == 'with_sidecar' else 'Baseline'} for {duration_s}s...")
    time.sleep(duration_s)

    if condition == "with_sidecar":
        stop_all(sidecars)

    # Stop collector
    resp = requests.post(f"{COLLECTOR_URL}/stop", timeout=10)
    print(f"   [Collector] {resp.json().get('records', '?')} records saved.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ROS2 topic jitter benchmark — with vs without attestation sidecar",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--condition", choices=["no_sidecar", "with_sidecar"], required=True)
    parser.add_argument("--runs",      type=int, default=1)
    parser.add_argument("--duration",  type=int, default=DEFAULT_DURATION_S,
                        help=f"Collection window in seconds (default: {DEFAULT_DURATION_S})")
    parser.add_argument("--ssp",       type=int,   default=_DEFAULT_SSP,
                        help=f"Attestation interval in milliseconds (default from .env: {_DEFAULT_SSP})")
    parser.add_argument("--iterq",    type=int,   default=_DEFAULT_ITERQ,
                        help=f"Rolling hash queue depth (default from .env: {_DEFAULT_ITERQ})")
    parser.add_argument("--robots",    type=int,   default=_DEFAULT_N_ROBOTS,
                        help=f"Number of robot sidecars to start/stop (default from .env: {_DEFAULT_N_ROBOTS}). "
                             f"Data is always collected from robot{COLLECTOR_ROBOT} only.")
    parser.add_argument("--cpu-limit", type=float, default=_DEFAULT_CPU_LIMIT,
                        help=f"Sidecar CPU limit (default from .env: {_DEFAULT_CPU_LIMIT!r}; "
                             f"None means uncapped, tagged as cpuNC)")
    parser.add_argument("--topic",     default="scan",
                        help="Topic suffix used for this run, e.g. 'scan' or 'odom'. "
                             "Must match TOPIC_NAME set when starting the collector. "
                             "Used only to organise output paths. (default: scan)")
    args = parser.parse_args()

    folder = "baseline" if args.condition == "no_sidecar" else "continuous"

    topic_slug = args.topic.lstrip("/").replace("/", "_") or "scan"

    sidecars       = build_sidecars(args.robots)
    robot_sidecars = {k: v for k, v in sidecars.items() if k != "secaas"}

    run_tag = _build_run_tag(args.ssp, args.iterq, args.cpu_limit) if args.condition == "with_sidecar" else None

    cpu_display = "uncapped (NC)" if args.cpu_limit is None else str(args.cpu_limit)
    print(f"📋 condition={args.condition}  folder={folder}  topic={topic_slug}  runs={args.runs}  duration={args.duration}s")
    if run_tag:
        print(f"📋 params={run_tag}  cpu={cpu_display}")
    print(f"📁 Output: {BASE_RESULTS_DIR / topic_slug / args.condition / folder}/")

    # Pre-flight: collector
    print("\n🔍 Checking topic collector...")
    if not wait_for_http(f"{COLLECTOR_URL}/status", timeout=30):
        print(f"❌ Collector not reachable at {COLLECTOR_URL}")
        sys.exit(1)
    print("✅ Collector ready.")

    # Pre-flight: sidecars (only for with_sidecar)
    if args.condition == "with_sidecar":
        print("🔍 Checking sidecars...")
        for name, port in sidecars.items():
            if not wait_for_http(f"http://localhost:{port}/", timeout=15):
                print(f"❌ Sidecar '{name}' not reachable.")
                sys.exit(1)
        print("✅ Sidecars ready.")

    for i in range(1, args.runs + 1):
        run_benchmark_loop(i, args.condition, folder, topic_slug, sidecars, robot_sidecars, run_tag=run_tag, duration_s=args.duration, ssp_ms=args.ssp if args.condition == "with_sidecar" else None)
        if i < args.runs:
            print("   [System] Cooldown 5s...")
            time.sleep(5)

    print(f"\n🎉 Done. Results in {BASE_RESULTS_DIR / topic_slug / args.condition / folder}/")


if __name__ == "__main__":
    main()
