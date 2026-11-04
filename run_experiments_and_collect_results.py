#!/usr/bin/env python3
import time
import argparse
import requests
import subprocess
import sys
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Configuration ---
MONITOR_URL      = "http://localhost:6000"
MONITOR_PORT     = 6000
SECAAS_URL       = "http://localhost:8000"   # SECaaS is always on local machine
SECAAS_PORT      = 8000
ROBOTS_BASE_PORT = 8001   # robot1=8001, robot2=8002, ...

# Multi-host defaults (overridable via CLI)
_DEFAULT_REMOTE1_HOST  = "10.5.1.20"   # remote host 1 (up to REMOTE1_LIMIT robots)
_DEFAULT_REMOTE2_HOST  = ""            # remote host 2 (placeholder)
_DEFAULT_REMOTE1_LIMIT = 64            # remote mode: robots 1-N are on remote1
REMOTE_USER            = "nextnet"
REMOTE_DIR             = "~/ros2-b-mutra"

# Absolute path so the script works regardless of the working directory
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# (host, port) tuple for SECaaS — always localhost
_SECAAS_HOST_PORT = ("localhost", SECAAS_PORT)


def _read_env_defaults():
    """Read SSP_ms, ITERQ, and CPU_LIMIT defaults from .env (set by start.sh)."""
    ssp_ms    = 20000
    iterq     = 1
    cpu_limit = None  # NC (no cap) by default
    env_path  = os.path.join(_SCRIPT_DIR, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if key == "ATTESTATION_INTERVAL_MS":
                    try:
                        ssp_ms = int(val)
                    except ValueError:
                        pass
                elif key == "CPU_LIMIT":
                    if val.upper() == "NC":
                        cpu_limit = None
                    else:
                        try:
                            cpu_limit = float(val)
                        except ValueError:
                            pass
                elif key == "ITERQ_THRESHOLD":
                    try:
                        iterq = int(val)
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return ssp_ms, iterq, cpu_limit


_DEFAULT_SSP, _DEFAULT_ITERQ, _DEFAULT_CPU_LIMIT = _read_env_defaults()


def build_sidecars(num_robots, remote1_host=None, remote2_host=None,
                   remote1_limit=_DEFAULT_REMOTE1_LIMIT):
    """Return {name: (host, port)} for SECaaS and all robot sidecars.

    Local mode (remote1_host=None): all robots on localhost.

    Remote mode (remote1_host set):
        - robots 1..min(num_robots, remote1_limit)  → remote1_host
        - robots remote1_limit+1..num_robots        → remote2_host (if provided)
    """
    sidecars = {"secaas": ("localhost", SECAAS_PORT)}
    for i in range(1, num_robots + 1):
        port = ROBOTS_BASE_PORT + i - 1
        if remote1_host:
            host = remote2_host if (i > remote1_limit and remote2_host) else remote1_host
        else:
            host = "localhost"
        sidecars[f"robot{i}"] = (host, port)
    return sidecars


def _get_monitor_groups(robot_sidecars):
    """Return {monitor_url: [container_names]} grouping sidecars by host.

    SECaaS always goes to the local monitor. Each unique remote host gets its
    own monitor entry derived from MONITOR_PORT.
    """
    groups = defaultdict(list)
    groups[MONITOR_URL].append("secaas")
    for name, (host, _) in robot_sidecars.items():
        mon = MONITOR_URL if host == "localhost" else f"http://{host}:{MONITOR_PORT}"
        groups[mon].append(f"{name}-sidecar")
    return dict(groups)


# --- Helper Functions ---

def wait_for_http(url, timeout=30):
    """Waits for an HTTP endpoint to become available."""
    print(f"⏳ Waiting for {url} ...", end="", flush=True)
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            requests.get(url, timeout=2)
            print(" ✅")
            return True
        except requests.RequestException:
            time.sleep(1)
            print(".", end="", flush=True)
    print("\n❌ Timeout waiting for service.")
    return False

def check_all_services(sidecars, robot_sidecars):
    """Verifies that ALL defined services are up and listening (parallel)."""
    print("\n🔍 Pre-flight check: Verifying all services...")

    checks = {"monitor": f"{MONITOR_URL}/monitor/status"}

    # Add remote monitors derived from robot sidecar hosts
    remote_mon_urls = {
        f"http://{host}:{MONITOR_PORT}"
        for _, (host, _) in robot_sidecars.items()
        if host != "localhost"
    }
    for i, url in enumerate(sorted(remote_mon_urls), 1):
        checks[f"remote-monitor-{i}"] = f"{url}/monitor/status"

    for name, (host, port) in sidecars.items():
        checks[name] = f"http://{host}:{port}/"

    all_ok = True
    with ThreadPoolExecutor(max_workers=len(checks)) as executor:
        futures = {executor.submit(wait_for_http, url): name for name, url in checks.items()}
        for future, name in futures.items():
            if not future.result():
                print(f"❌ Service '{name}' is not responding.")
                all_ok = False

    if all_ok:
        print("🚀 All systems GO.\n")
    return all_ok

def trigger_action(name, host_port, action, timeout=5):
    """Sends start/stop signals to the sidecar API."""
    host, port = host_port
    try:
        url = f"http://{host}:{port}/{action}"
        resp = requests.post(url, timeout=timeout)
        resp.raise_for_status()
        return f"✔ {name}: {action.upper()}"
    except Exception as e:
        return f"❌ {name}: Failed to {action} ({e})"

def get_status(name, host_port):
    """Returns the status string from /status endpoint."""
    host, port = host_port
    try:
        resp = requests.get(f"http://{host}:{port}/status", timeout=5)
        resp.raise_for_status()
        return resp.json().get("status", "unknown")
    except Exception as e:
        return f"error: {e}"

def _poll_statuses_parallel(robot_sidecars):
    """Fetches /status from all robot sidecars in parallel."""
    with ThreadPoolExecutor(max_workers=len(robot_sidecars)) as executor:
        futures = {executor.submit(get_status, name, hp): name for name, hp in robot_sidecars.items()}
        return {futures[f]: f.result() for f in as_completed(futures)}

def wait_for_all_running(robot_sidecars, timeout=30):
    """Waits until all robot sidecars have left 'idle' (i.e. report 'running'
    or already 'finished').
    """
    print(f"   ⏳ Waiting for all agents to start running (timeout={timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        statuses = _poll_statuses_parallel(robot_sidecars)
        all_started = all(s in ("running", "finished") for s in statuses.values())
        if all_started:
            print("   ✅ All agents running.")
            return True
        pending = [n for n, s in statuses.items() if s not in ("running", "finished")]
        print(f"      Still waiting to start: {pending}")
        time.sleep(0.5)
    print("   ❌ Timeout: not all agents started in time.")
    return False

def wait_for_all_finished(robot_sidecars, timeout=120):
    """Polls all robot sidecars until all report 'finished' or timeout."""
    print(f"   ⏳ Waiting for all agents to finish (timeout={timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        statuses = _poll_statuses_parallel(robot_sidecars)
        all_done = all(s == "finished" for s in statuses.values())
        if all_done:
            print("   ✅ All agents finished.")
            return True
        pending = [n for n, s in statuses.items() if s != "finished"]
        print(f"      Still waiting: {pending}")
        time.sleep(2)
    print("   ❌ Timeout: not all agents finished in time.")
    return False

def set_config(name, host_port, payload: dict):
    """Sends a partial config update to a single sidecar via POST /config."""
    host, port = host_port
    try:
        resp = requests.post(f"http://{host}:{port}/config", json=payload, timeout=5)
        resp.raise_for_status()
        applied = resp.json().get("applied", payload)
        return f"✔ {name}: {applied}"
    except Exception as e:
        return f"❌ {name}: Failed to set config ({e})"

def set_config_all(robot_sidecars, payload: dict):
    """Sends a partial config update to all robot sidecars in parallel.
    SECaaS is excluded — it has no /config endpoint and is always event-driven.
    """
    print(f"   [Config] Pushing to all robot sidecars: {payload}...")
    with ThreadPoolExecutor(max_workers=len(robot_sidecars)) as executor:
        futures = [executor.submit(set_config, n, hp, payload) for n, hp in robot_sidecars.items()]
        for f in as_completed(futures):
            print(f"      {f.result()}")

def stop_all_agents(sidecars):
    """Stops all agents in parallel."""
    print("   [Agents] Stopping...")
    with ThreadPoolExecutor(max_workers=len(sidecars)) as executor:
        futures = [executor.submit(trigger_action, n, hp, "stop", 15) for n, hp in sidecars.items()]
        for f in as_completed(futures):
            print(f"      {f.result()}")

# V2 and V3: only their admins may reset them (the keys start.sh anchors the references with).
_ADMIN_KEYS = [os.path.join(_SCRIPT_DIR, "blockchain/quorum-test-network/config/nodes", v, "accountPrivateKey")
               for v in ("validator2", "validator3")]

def _env_value(key):
    """Value of <key> in .env, written by start.sh."""
    with open(os.path.join(_SCRIPT_DIR, ".env")) as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and k.strip() == key:
                return v.strip()
    raise SystemExit(f"{key} is missing from .env: run start.sh first")

def reset_chain():
    """Resets the on-chain attestation chain: through the admins for V2 and V3, through the SECaaS otherwise."""
    contract = _env_value("CONTRACT")
    if contract in ("AttestationManagerV2", "AttestationManagerV3"):
        subprocess.run([os.path.join(_SCRIPT_DIR, "reset_chain.sh"), contract, *_ADMIN_KEYS], check=True)
    else:
        requests.post(f"{SECAAS_URL}/reset", timeout=10).raise_for_status()
    print("   [Chain] State reset.")


def _build_run_tag(ssp_ms, iterq, cpu_limit) -> str:
    """Build the param-tag string embedded in output filenames, e.g. 'SSP20000ms-ITERQ1-cpu0p4'."""
    if ssp_ms is None or iterq is None:
        return ""
    cpu_str = "NC" if cpu_limit is None else f"{cpu_limit:.1f}".replace(".", "p")
    return f"SSP{ssp_ms}ms-ITERQ{iterq}-cpu{cpu_str}"


def run_experiment_loop(run_id, duration, stats_dir, attestation_dir, sidecars, robot_sidecars,
                        startup_mode=False, ssp_ms=None, iterq=None, cpu_limit=None,
                        startup_timeout=180):
    run_tag = _build_run_tag(ssp_ms, iterq, cpu_limit)
    print(f"\n==============================\n🏁 Run {run_id}\n==============================")

    # 1. Push common config to all robot sidecars, then set per-robot results_file (continuous only)
    set_config_all(robot_sidecars, {"one_shot": startup_mode, "export_enabled": True, "results_dir": attestation_dir})
    set_config("secaas", _SECAAS_HOST_PORT, {"export_enabled": True, "results_dir": attestation_dir})

    if run_tag:
        print(f"   [Config] Setting per-robot results_file ({run_tag})...")
        def _set_rf(name, hp):
            fname = f"{attestation_dir}/{name}-{run_tag}-run{run_id}.json"
            return set_config(name, hp, {"results_file": fname})
        with ThreadPoolExecutor(max_workers=len(robot_sidecars)) as executor:
            for f in as_completed([executor.submit(_set_rf, n, hp) for n, hp in robot_sidecars.items()]):
                print(f"      {f.result()}")
        secaas_rf = f"{attestation_dir}/secaas-{run_tag}-run{run_id}.json"
        print(f"      {set_config('secaas', _SECAAS_HOST_PORT, {'results_file': secaas_rf})}")

    # 2. Start Docker Monitor(s) — one per unique host (local + any remote hosts)
    # Collectors auto-increment run numbers per param combination; pass params, not explicit names.
    monitor_groups = _get_monitor_groups(robot_sidecars)
    print(f"   [Monitor] Starting stats -> {stats_dir}")
    for mon_url, containers in monitor_groups.items():
        try:
            payload = {"containers": containers, "interval": 1.0, "csv_dir": stats_dir}
            if run_tag:
                payload.update({"ssp_ms": ssp_ms, "iterq": iterq, "cpu_limit": cpu_limit})
            requests.post(f"{mon_url}/monitor/start", json=payload, timeout=5)
        except Exception as e:
            print(f"   ⚠️ Monitor {mon_url} start failed: {e}")

    # Allow monitor to complete one clean baseline sample before agents start
    time.sleep(1)

    # 3. Start Agents (Parallel)
    print("   [Agents] Starting...")
    with ThreadPoolExecutor(max_workers=len(sidecars)) as executor:
        futures = [executor.submit(trigger_action, n, hp, "start") for n, hp in sidecars.items()]
        for f in as_completed(futures):
            print(f"      {f.result()}")

    # 4. Wait until all robots have left 'idle'
    wait_for_all_running(robot_sidecars, timeout=30)

    if startup_mode:
        wait_for_all_finished(robot_sidecars, timeout=startup_timeout)
        stop_all_agents(sidecars)
        time.sleep(5)
        reset_chain()
    else:
        print(f"   ⏳ Running for {duration}s...")
        time.sleep(duration)
        stop_all_agents(sidecars)
        time.sleep(2)

    # Stop Monitor(s)
    for mon_url in monitor_groups:
        try:
            requests.post(f"{mon_url}/monitor/stop", timeout=60)
        except Exception:
            pass


def run_continuous_warmup(sidecars, robot_sidecars, startup_timeout=180):
    """One-shot warmup run with export disabled to seed lastSuccess on-chain."""
    print("\n🔥 Continuous warmup: seeding lastSuccess via one-shot run (export disabled)...")

    set_config_all(robot_sidecars, {"one_shot": True, "export_enabled": False})
    set_config("secaas", _SECAAS_HOST_PORT, {"export_enabled": False})

    print("   [Warmup] Starting agents...")
    with ThreadPoolExecutor(max_workers=len(sidecars)) as executor:
        futures = [executor.submit(trigger_action, n, hp, "start") for n, hp in sidecars.items()]
        for f in as_completed(futures):
            print(f"      {f.result()}")

    wait_for_all_running(robot_sidecars, timeout=30)
    wait_for_all_finished(robot_sidecars, timeout=startup_timeout)
    stop_all_agents(sidecars)

    print("   ✅ Warmup complete. lastSuccess seeded for all robots.\n")


_SUDO_PASSWORD = "netcom;"

def reset_checkpoints(remote1_host=None, remote2_host=None,
                      remote1_limit=_DEFAULT_REMOTE1_LIMIT, num_robots=0):
    """Clears all event watcher checkpoint JSON files (local + remote hosts as applicable)."""
    base = os.path.join(_SCRIPT_DIR, "checkpoints")
    reset_script = os.path.join(base, "reset_event_watcher.sh")
    print("   [Checkpoints] Clearing local event watcher state...")
    if os.path.isfile(reset_script):
        subprocess.run(
            f"echo {_SUDO_PASSWORD!r} | sudo -S bash {reset_script}",
            shell=True, cwd=base, check=True,
        )
    else:
        subprocess.run(
            f"echo {_SUDO_PASSWORD!r} | sudo -S find {base} -type f -name '*.json' -delete",
            shell=True, check=True,
        )

    if remote1_host:
        print(f"   [Checkpoints] Clearing remote1 ({remote1_host}) event watcher state...")
        subprocess.run(
            ["ssh", f"{REMOTE_USER}@{remote1_host}",
             f"find {REMOTE_DIR}/checkpoints -type f -name '*.json' -delete 2>/dev/null || true"],
            check=True,
        )

    if remote2_host and num_robots > remote1_limit:
        print(f"   [Checkpoints] Clearing remote2 ({remote2_host}) event watcher state...")
        subprocess.run(
            ["ssh", f"{REMOTE_USER}@{remote2_host}",
             f"find {REMOTE_DIR}/checkpoints -type f -name '*.json' -delete 2>/dev/null || true"],
            check=True,
        )

    print("   [Checkpoints] Done.")


def sync_remote_results(remote_hosts: list):
    """Rsync results/ subdirs from each remote host, never overwriting existing files."""
    for remote_host in remote_hosts:
        for subdir in ["attestation-times/results", "docker-stats/results"]:
            local_dst  = os.path.join(_SCRIPT_DIR, "experiments", "data", subdir) + "/"
            remote_src = f"{REMOTE_USER}@{remote_host}:{REMOTE_DIR}/experiments/data/{subdir}/"
            print(f"\n📥 Syncing {remote_src} → {local_dst}")
            os.makedirs(local_dst, exist_ok=True)
            subprocess.run(
                ["rsync", "-av", "--ignore-existing",
                 "--no-owner", "--no-group", "--no-perms",
                 remote_src, local_dst],
                check=True,
            )
    print("✅ Remote results synced.")


def run_experiments(runs, duration, stats_dir, attestation_dir, sidecars, robot_sidecars,
                    startup_mode, remote1_host=None, remote2_host=None,
                    remote1_limit=_DEFAULT_REMOTE1_LIMIT, num_robots=0,
                    ssp_ms=None, iterq=None, cpu_limit=None, startup_timeout=180):
    reset_checkpoints(remote1_host=remote1_host, remote2_host=remote2_host,
                      remote1_limit=remote1_limit, num_robots=num_robots)

    # Pre-set SECaaS results_dir immediately so any stale blockchain events from a
    # previous session land in the correct directory, not the old one.
    set_config("secaas", _SECAAS_HOST_PORT, {"results_dir": attestation_dir})

    if not startup_mode:
        run_continuous_warmup(sidecars, robot_sidecars, startup_timeout=startup_timeout)
        try:
            requests.post(f"{SECAAS_URL}/secaas-only?enabled=false", timeout=10)
            print("   [Chain] SECaaS-only mode disabled. RR verifier election active.")
        except Exception as e:
            print(f"   [Chain] ⚠️ Warning: Failed to disable secaas-only: {e}")
        set_config_all(robot_sidecars, {"export_enabled": True})
        set_config("secaas", _SECAAS_HOST_PORT, {"export_enabled": True})
        print("   [System] Cooling down for 5s after warmup...")
        time.sleep(5)

    for i in range(1, runs + 1):
        run_experiment_loop(i, duration, stats_dir, attestation_dir, sidecars, robot_sidecars,
                            startup_mode=startup_mode,
                            ssp_ms=ssp_ms, iterq=iterq, cpu_limit=cpu_limit,
                            startup_timeout=startup_timeout)
        if i < runs:
            print("   [System] Cooling down for 5s...")
            time.sleep(5)


# --- Main ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Data collection — two modes: startup (one-shot) or continuous (fixed duration)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--robots",       type=int, default=4,
                        help="Number of robot sidecars (default: 4, ports 8001..800N)")
    parser.add_argument("--runs",         type=int, default=1,
                        help="Number of runs (default: 1)")
    parser.add_argument("--remote",        action="store_true",
                        help="Remote mode: all robots run on remote1 (and remote2 if N > remote1-limit). "
                             "Default (local): everything on localhost.")
    parser.add_argument("--remote1-host",  default=None,
                        help=f"IP of remote host 1 (default: {_DEFAULT_REMOTE1_HOST})")
    parser.add_argument("--remote2-host",  default=None,
                        help="IP of remote host 2 for robots beyond --remote1-limit "
                             f"(default: '{_DEFAULT_REMOTE2_HOST or 'not set'}')")
    parser.add_argument("--remote1-limit", type=int, default=_DEFAULT_REMOTE1_LIMIT,
                        help=f"Remote mode: robots 1-N on remote1; rest on remote2 "
                             f"(default: {_DEFAULT_REMOTE1_LIMIT})")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--startup",  action="store_true",
                      help="One-shot mode: poll until all agents report 'finished'")
    mode.add_argument("--duration", type=int,
                      help="Continuous mode: run for this many seconds then stop agents")

    parser.add_argument("--startup-timeout", type=int, default=180,
                        help="Max seconds to wait for all agents to finish in startup/warmup mode (default: 180)")

    # Experiment parameters — defaults read from .env (set by start.sh)
    parser.add_argument("--ssp",       type=int,   default=_DEFAULT_SSP,
                        help=f"Attestation interval in milliseconds — SSP (default from .env: {_DEFAULT_SSP})")
    parser.add_argument("--iterq",    type=int,   default=_DEFAULT_ITERQ,
                        help=f"Rolling hash queue depth — ITERQ (default from .env: {_DEFAULT_ITERQ})")
    def _cpu_type(val):
        return None if val.upper() == "NC" else float(val)
    _cpu_default_str = "NC" if _DEFAULT_CPU_LIMIT is None else str(_DEFAULT_CPU_LIMIT)
    parser.add_argument("--cpu-limit", type=_cpu_type, default=_DEFAULT_CPU_LIMIT,
                        help=f"Sidecar CPU limit, or 'NC' for no cap (default from .env: {_cpu_default_str})")
    parser.add_argument("--variant",   default="rr",
                        choices=["rr", "lv"],
                        help="Continuous-mode variant subdirectory (default: rr)")

    args = parser.parse_args()

    # Resolve deployment topology
    remote1_limit = args.remote1_limit

    # Resolve remote1_host (only used in remote mode)
    remote1_host = None
    if args.remote:
        remote1_host = args.remote1_host or _DEFAULT_REMOTE1_HOST
        print(f"ℹ️  Remote mode: using remote1={remote1_host} for all {args.robots} robot(s)")

    # Resolve remote2_host
    remote2_host = args.remote2_host or (_DEFAULT_REMOTE2_HOST if _DEFAULT_REMOTE2_HOST else None)
    if args.remote and args.robots > remote1_limit:
        if not remote2_host:
            print(f"❌ N={args.robots} > remote1-limit={remote1_limit} but no remote2-host configured.")
            sys.exit(1)
        print(f"ℹ️  N={args.robots} > remote1-limit={remote1_limit}: "
              f"using remote2={remote2_host} for robots {remote1_limit + 1}–{args.robots}")

    robot_label = f"N{args.robots}"

    if args.startup:
        ssp_ms, iterq, cpu_limit = None, None, None
        stats_dir       = f"/experiments/data/docker-stats/results/{robot_label}/startup"
        attestation_dir = f"/experiments/data/attestation-times/results/{robot_label}/startup"
    else:
        ssp_ms, iterq, cpu_limit = args.ssp, args.iterq, args.cpu_limit
        variant  = args.variant
        stats_dir       = f"/experiments/data/docker-stats/results/{robot_label}/continuous/{variant}"
        attestation_dir = f"/experiments/data/attestation-times/results/{robot_label}/continuous/{variant}"

    # Pre-create local result directories so Docker volume mounts don't create
    # them as root (which would block the post-run rsync with Permission Denied).
    for container_path in [stats_dir, attestation_dir]:
        local_path = os.path.join(_SCRIPT_DIR, container_path.lstrip("/"))
        os.makedirs(local_path, exist_ok=True)

    sidecars = build_sidecars(args.robots, remote1_host=remote1_host, remote2_host=remote2_host,
                              remote1_limit=remote1_limit)
    robot_sidecars = {k: v for k, v in sidecars.items() if k != "secaas"}

    print(f"📋 Configured sidecars:")
    for name, (host, port) in sidecars.items():
        print(f"   {name}: http://{host}:{port}")
    if args.startup:
        print("🔁 Mode: STARTUP (one-shot)")
    else:
        print(f"🔁 Mode: CONTINUOUS ({args.duration}s per run)  variant={args.variant}  {_build_run_tag(ssp_ms, iterq, cpu_limit)}")
    print(f"📁 Stats dir:       {stats_dir}")
    print(f"📁 Attestation dir: {attestation_dir}")

    if not check_all_services(sidecars, robot_sidecars):
        print("🛑 Aborting: Not all services are ready.")
        sys.exit(1)

    try:
        run_experiments(args.runs, args.duration or 0, stats_dir, attestation_dir,
                        sidecars, robot_sidecars, startup_mode=args.startup,
                        remote1_host=remote1_host, remote2_host=remote2_host,
                        remote1_limit=remote1_limit, num_robots=args.robots,
                        ssp_ms=ssp_ms, iterq=iterq, cpu_limit=cpu_limit,
                        startup_timeout=args.startup_timeout)
    except KeyboardInterrupt:
        print("\n🛑 Interrupted by user.")

    print(f"\n🎉 All runs completed. Results saved to {stats_dir}")

    # Sync results from any remote hosts used
    remote_hosts_used = [h for h in [remote1_host, remote2_host] if h]
    if remote_hosts_used:
        sync_remote_results(remote_hosts_used)

    # Remove all secaas.json files anywhere under the attestation results tree.
    # SECaaS exports its own JSON on every run; it's not useful for analysis.
    results_root = os.path.join(_SCRIPT_DIR, "experiments", "data", "attestation-times", "results")
    result = subprocess.run(
        ["find", results_root, "-type", "f", "-name", "secaas.json", "-delete", "-print"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        for f in result.stdout.strip().splitlines():
            print(f"🧹 Removed ghost artifact: {f}")
