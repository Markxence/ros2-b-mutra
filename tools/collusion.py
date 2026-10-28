#!/usr/bin/env python3
"""Collusion experiment (V3): robot1 is tampered, robots 2..f+1 are lying
verifiers (FORCE_VERDICT=valid), the others are honest. Counts robot1's cycles
that are wrongly accepted, and compares with the hypergeometric prediction.

Usage: ./tools/collusion.py OUT_DIR RUN [RUN...]   with RUN = jury:quorum:f:minutes
       e.g. ./tools/collusion.py /root/measure/collusion 1:1:2:20 3:2:2:40
Starts and stops the stack for each run; appends one JSON line per run to
OUT_DIR/results.jsonl and keeps the sidecar and SECaaS logs in OUT_DIR/<run>/.
"""
import json
import os
import re
import subprocess
import sys
import time
from math import comb

N = 16
SSP_MS = 10000
SKIP = 2  # cycles after the tamper that may carry a measurement taken before it
ANSI = re.compile(r"\x1b\[[0-9;]*m")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sh(*cmd, env=None):
    subprocess.run(cmd, cwd=ROOT, check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def logs(container):
    out = subprocess.run(["docker", "logs", container], capture_output=True, text=True, check=True)
    return [ANSI.sub("", l) for l in (out.stdout + out.stderr).splitlines()]


def prover_cycles(lines):
    """robot1's cycles in order. The prover runs one cycle at a time, so the
    lines of a cycle are read in sequence, without relying on the short id."""
    cycles, cur = [], None
    for l in lines:
        if "[Prover-" not in l:
            continue
        if "Evidence sent" in l:
            cur = {"result": None, "by": None, "votes": None}
            cycles.append(cur)
        elif cur is None:
            continue
        elif "Result:" in l:
            cur["result"] = "SUCCESS" if "SUCCESS" in l else "FAILURE"
        elif "Verified by:" in l:
            cur["by"] = "jury" if "the jury" in l else "secaas"
        elif "Jury votes:" in l:
            cur["votes"] = l.split("Jury votes: ", 1)[1]
    return cycles


def predicted(jury, quorum, f, pool):
    return sum(comb(f, k) * comb(pool - f, jury - k) for k in range(quorum, jury + 1)) / comb(pool, jury)


def run(out_dir, jury, quorum, f, minutes):
    name = f"j{jury}q{quorum}f{f}"
    env = dict(os.environ, TERM="xterm")
    for i in range(2, f + 2):
        env[f"FORCE_VERDICT_ROBOT{i}"] = "valid"
    if subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, check=True).stdout.strip():
        raise SystemExit(f"{name}: containers are already running, stop them first")
    sh("./start.sh", "--robots", str(N), "--contract", "v3", "--jury", str(jury), "--quorum", str(quorum),
       "--ssp", str(SSP_MS), "--iterq", "1", "--auto", env=env)
    try:
        deadline = time.time() + 300
        while not all(any("Result: ✅ SUCCESS" in l for l in logs(f"robot{i}-sidecar")) for i in range(1, N + 1)):
            if time.time() > deadline:
                raise SystemExit(f"{name}: not every robot attested successfully within 300 s")
            time.sleep(5)
        liars = [i for i in range(2, f + 2)
                 if not any("FORCE_VERDICT is set" in l for l in logs(f"robot{i}-sidecar"))]
        if liars:
            raise SystemExit(f"{name}: FORCE_VERDICT not active on robots {liars}")
        before = len(prover_cycles(logs("robot1-sidecar")))
        sh("./tools/tamper.sh", "robot1")
        time.sleep(minutes * 60)
        d = os.path.join(out_dir, name)
        os.makedirs(d, exist_ok=True)
        for c in [f"robot{i}-sidecar" for i in range(1, N + 1)] + ["secaas"]:
            with open(os.path.join(d, c + ".log"), "w") as fh:
                fh.write("\n".join(logs(c)) + "\n")
    finally:
        sh("./stop.sh", env=env)
    cycles = [c for c in prover_cycles(open(os.path.join(d, "robot1-sidecar.log")).read().splitlines())[before + SKIP:]
              if c["result"]]
    accepted = sum(c["result"] == "SUCCESS" for c in cycles)
    row = dict(run=name, jury=jury, quorum=quorum, liars=f, robots=N, pool=N - 1, minutes=minutes,
               cycles=len(cycles), accepted=accepted,
               by_jury=sum(c["by"] == "jury" for c in cycles), by_secaas=sum(c["by"] == "secaas" for c in cycles),
               measured=round(accepted / len(cycles), 4) if cycles else None,
               predicted=round(predicted(jury, quorum, f, N - 1), 4))
    with open(os.path.join(out_dir, "results.jsonl"), "a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    for spec in sys.argv[2:]:
        j, q, f, m = (int(x) for x in spec.split(":"))
        run(out, j, q, f, m)
