#!/usr/bin/env python3
"""
TOCTOU campaign: how often is a tamper-run-restore attack of duration tau caught?

The attacker is informed: it waits for the end of a measurement (the prover's
"Final digest" log line, which comes just before the SendEvidence that makes the
measurement visible on chain), tampers the robot binary at once, and restores
the original bytes tau later (tools/tamper.sh --restore-after). The attack is
detected when a measurement taken between the end of that measurement and the
restore leads to a FAILURE result.

Each robot runs one (SSP, mode) point, all robots at the same time: the points
of --ssp x --modes go to robot1, robot2, ... in that order. For each point the
trials are the ratios tau/SSP times --trials, in a shuffled order.

Prerequisites: stack started with --auto, --iterq 1 and as many robots as points,
e.g. ./start.sh --robots 4 --contract lv --iterq 1 --auto

One CSV per SSP under experiments/data/toctou/results/, one row per trial.

Usage:
  python3 run_experiments_toctou.py --ssp 5000,20000 --modes fixed,exp \\
      --ratios 0.05,0.1,0.25,0.5,1,2 --trials 30
"""
import argparse, csv, itertools, random, re, subprocess, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent
RESULTS = REPO_ROOT / "experiments" / "data" / "toctou" / "results"
ROBOTS_BASE_PORT = 8001  # robot1 = 8001, robot2 = 8002, ...

LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\b")
DIGEST_RE = re.compile(r"\[Prover\].*(?:Final digest|Rolling-hash digest)")
MEASURE_RE = re.compile(r"\[Prover-([^\]]+)\] Measurement \d+/\d+ at (\d+) ms")
RESULT_RE = re.compile(r"\[Prover-([^\]]+)\] Result: .*(SUCCESS|FAILURE)")
TAMPERED_RE = re.compile(r"tampered .* at (\d+) ms")
RESTORED_RE = re.compile(r"restored the original bytes at (\d+) ms")


def log_ms(line):
    m = LOG_TS_RE.match(line)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)  # container logs are UTC


class SidecarLog:
    """Follows a sidecar log: measurement ends, measurement starts by attestation, results."""

    def __init__(self, container):
        self.cond = threading.Condition()
        self.digests = []      # ms of each "Final digest" line
        self.measures = {}     # attestation prefix -> start ms
        self.results = {}      # attestation prefix -> "SUCCESS" | "FAILURE"
        self.proc = subprocess.Popen(["docker", "logs", "-f", "--since", "1s", container],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            with self.cond:
                if DIGEST_RE.search(line):
                    self.digests.append(log_ms(line))
                elif m := MEASURE_RE.search(line):
                    self.measures[m.group(1)] = int(m.group(2))
                elif m := RESULT_RE.search(line):
                    self.results[m.group(1)] = m.group(2)
                else:
                    continue
                self.cond.notify_all()

    def next_digest_after(self, ms, timeout):
        with self.cond:
            ok = self.cond.wait_for(lambda: self.digests and self.digests[-1] > ms, timeout)
            return self.digests[-1] if ok else None

    def results_between(self, start_ms, end_ms, timeout):
        """Results of the measurements started in (start_ms, end_ms], once all of them are in."""
        def ids():
            return [a for a, t in self.measures.items() if start_ms < t <= end_ms]
        with self.cond:
            ok = self.cond.wait_for(lambda: all(a in self.results for a in ids()), timeout)
            return [self.results.get(a) for a in ids()], ok


def configure(port, ssp_ms, mode):
    """/config answers 409 while the loop runs: stop, set, start."""
    base = f"http://localhost:{port}"
    requests.post(f"{base}/stop", timeout=10).raise_for_status()
    time.sleep(0.5)
    r = requests.post(f"{base}/config", json={"attestation_interval_ms": ssp_ms, "attestation_interval_mode": mode}, timeout=10)
    r.raise_for_status()
    requests.post(f"{base}/start", timeout=10).raise_for_status()


def tamper(robot, tau_ms):
    out = subprocess.run([str(REPO_ROOT / "tools" / "tamper.sh"), robot, "--restore-after", str(tau_ms)],
                         capture_output=True, text=True, timeout=tau_ms / 1000 + 60)
    t, r = TAMPERED_RE.search(out.stdout), RESTORED_RE.search(out.stdout)
    if out.returncode != 0 or not (t and r):
        raise RuntimeError(f"tamper.sh failed on {robot}: {out.stdout}{out.stderr}")
    return int(t.group(1)), int(r.group(1))


def run_point(robot, ssp_ms, mode, ratios, trials, writer, lock, stop):
    log = SidecarLog(f"{robot}-sidecar")
    plan = [r for r in ratios for _ in range(trials)]
    random.shuffle(plan)
    for n, ratio in enumerate(plan, 1):
        if stop.is_set():
            return
        tau_ms = round(ratio * ssp_ms)
        # Anchor on a measurement that ends from now on: one that ended while we
        # waited for the previous results would make the attacker late.
        anchor = log.next_digest_after(int(time.time() * 1000), timeout=10 * ssp_ms / 1000 + 60)
        if anchor is None:
            raise RuntimeError(f"{robot}: no measurement for {10 * ssp_ms / 1000 + 60:.0f} s")
        tampered, restored = tamper(robot, tau_ms)
        time.sleep(2)  # let the log reader catch up with measurements started just before the restore
        results, complete = log.results_between(anchor, restored, timeout=120)
        row = dict(robot=robot, ssp_ms=ssp_ms, mode=mode, ratio=ratio, tau_ms=tau_ms,
                   anchor_ms=anchor, tampered_ms=tampered, restored_ms=restored,
                   measurements=len(results), failures=results.count("FAILURE"),
                   detected=int("FAILURE" in results), complete=int(complete))
        with lock:
            writer(row)
            print(f"{robot} SSP {ssp_ms} {mode} {n}/{len(plan)}: tau/SSP {ratio}, "
                  f"{len(results)} measurement(s), {'detected' if row['detected'] else 'missed'}", flush=True)


def csv_writer(ssps):
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = {s: RESULTS / f"SSP{s}ms-{stamp}.csv" for s in ssps}

    def write(row):
        path = paths[row["ssp_ms"]]
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
    return write, paths


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ssp", required=True, help="comma-separated SSPs in ms, e.g. 5000,20000")
    p.add_argument("--modes", default="fixed,exp", help="comma-separated modes among fixed, exp")
    p.add_argument("--ratios", required=True, help="comma-separated tau/SSP values, e.g. 0.05,0.1,0.5,1,2")
    p.add_argument("--trials", type=int, required=True, help="trials per ratio and point")
    a = p.parse_args()
    ssps = [int(x) for x in a.ssp.split(",")]
    modes = a.modes.split(",")
    if not set(modes) <= {"fixed", "exp"}:
        sys.exit(f"--modes must be among fixed, exp (got {a.modes})")
    ratios = [float(x) for x in a.ratios.split(",")]
    points = list(itertools.product(ssps, modes))

    write, paths = csv_writer(ssps)
    lock, stop, threads, errors = threading.Lock(), threading.Event(), [], []
    for i, (ssp, mode) in enumerate(points, 1):
        configure(ROBOTS_BASE_PORT + i - 1, ssp, mode)
        print(f"robot{i}: SSP {ssp} ms, mode {mode}", flush=True)

    def worker(i, ssp, mode):
        try:
            run_point(f"robot{i}", ssp, mode, ratios, a.trials, write, lock, stop)
        except Exception as e:  # stop the others: a broken point makes the campaign unusable
            errors.append(f"robot{i}: {e}")
            stop.set()

    for i, (ssp, mode) in enumerate(points, 1):
        t = threading.Thread(target=worker, args=(i, ssp, mode))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    for path in paths.values():
        print(f"CSV: {path}")
    if errors:
        sys.exit("FAILED: " + "; ".join(errors))


if __name__ == "__main__":
    main()
