#!/usr/bin/env python3
"""Cycle time and transactions per cycle on the local stack.

Usage: tools/measure_cycle.py <label> <start.sh arguments...>
Starts the stack with 4 robots, waits WARMUP s, measures for WINDOW s, stops
it and prints one JSON line. Cycle times come from the provers' logs, the
transactions from the blocks of the window (reverted ones included). Ledger
growth: the RLP size of the blocks of the window, and the growth of the data
directory of one validator.
"""
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

from eth_utils import keccak

N, WARMUP, WINDOW = 4, 60, 300
RPC = "http://localhost:21001"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGS = ["SendEvidence(bytes32,bytes32,uint32)", "Vote(bytes32,bool)", "Expire(bytes32)",
        "CloseAttestationProcess(bytes32,bool)", "ResolveAttestationSECaaS(bytes32,bool)"]
SELECTOR = {"0x" + keccak(text=s)[:4].hex(): s.split("(")[0] for s in SIGS}
COMPLETED = "0x" + keccak(text="AttestationCompleted(bytes32)").hex()
DONE = re.compile(r"\[Prover-[^\]]+\] completed in ([\d.]+)s")
VALIDATOR = "quorum-test-network-validator1-1"


def datadir_bytes():
    out = subprocess.run(["docker", "exec", VALIDATOR, "du", "-sb", "/opt/besu/data"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.split()[0])


def rpc(method, *params):
    req = urllib.request.Request(RPC, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode(),
                                 {"Content-Type": "application/json"})
    reply = json.load(urllib.request.urlopen(req))
    if "error" in reply:
        raise RuntimeError(f"{method}: {reply['error']}")
    return reply["result"]


def measure(label, contract):
    d0, b0, t0 = datadir_bytes(), int(rpc("eth_blockNumber"), 16), datetime.now(timezone.utc)
    time.sleep(WINDOW)
    d1, b1, t1 = datadir_bytes(), int(rpc("eth_blockNumber"), 16), datetime.now(timezone.utc)

    durations = []
    for i in range(1, N + 1):
        logs = subprocess.run(["docker", "logs", "--since", t0.isoformat(), "--until", t1.isoformat(), f"robot{i}-sidecar"],
                              capture_output=True, text=True, check=True)
        durations += [float(d) for d in DONE.findall(logs.stdout + logs.stderr)]

    closes = rpc("eth_getLogs", {"fromBlock": hex(b0 + 1), "toBlock": hex(b1), "address": contract, "topics": [COMPLETED]})
    sent, reverted, gas, block_bytes, txs = {}, {}, 0, 0, 0
    for b in range(b0 + 1, b1 + 1):
        block = rpc("eth_getBlockByNumber", hex(b), True)
        block_bytes += int(block["size"], 16)
        txs += len(block["transactions"])
        for tx in block["transactions"]:
            if (tx["to"] or "").lower() != contract.lower():
                continue
            fn = SELECTOR.get(tx["input"][:10], tx["input"][:10])
            receipt = rpc("eth_getTransactionReceipt", tx["hash"])
            sent[fn] = sent.get(fn, 0) + 1
            reverted[fn] = reverted.get(fn, 0) + (receipt["status"] != "0x1")
            gas += int(receipt["gasUsed"], 16)

    n = len(closes)
    return {
        "label": label, "seconds": round((t1 - t0).total_seconds()), "blocks": b1 - b0,
        "cycles_closed": n, "prover_cycles": len(durations),
        "cycle_s_mean": round(statistics.mean(durations), 2), "cycle_s_median": round(statistics.median(durations), 2),
        "cycle_s_p95": round(statistics.quantiles(durations, n=20)[-1], 2),
        "tx_per_cycle": round(sum(sent.values()) / n, 2), "reverted_per_cycle": round(sum(reverted.values()) / n, 2),
        "gas_per_cycle": round(gas / n), "tx_per_block": round(txs / (b1 - b0), 2),
        "block_bytes": block_bytes, "datadir_bytes": d1 - d0, "tx_by_function": sent, "reverted_by_function": {k: v for k, v in reverted.items() if v},
    }


def main():
    label, args = sys.argv[1], sys.argv[2:]
    env = dict(os.environ, TERM="xterm")
    subprocess.run(["./start.sh", "--robots", str(N), *args, "--auto"], cwd=REPO, env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with open(os.path.join(REPO, "config-dummy", "robot1.json")) as f:
            contract = json.load(f)["contract_address"]
        time.sleep(WARMUP)
        print(json.dumps(measure(label, contract)), flush=True)
    finally:
        subprocess.run(["./stop.sh"], cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
