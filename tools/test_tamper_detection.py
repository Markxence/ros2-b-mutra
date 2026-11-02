#!/usr/bin/env python3
"""Offline check of run_experiments_and_collect_tamper_detection.py: contract
from .env, verifier line parsing and tx_path per contract. No stack needed.

  python3 tools/test_tamper_detection.py
"""
import importlib.util
import pathlib
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("td", ROOT / "run_experiments_and_collect_tamper_detection.py")
td = importlib.util.module_from_spec(spec)
spec.loader.exec_module(td)

td.VERIFIER_LABELS = {"0xaaa": "secaas", "0xbbb": "robot2"}
jury = "2026-11-02 10:00:00.000 - INFO - [Prover-ab12] Verified by: the jury, closing vote from 0xBBB at 1"
plain = "2026-11-02 10:00:00.000 - INFO - [Prover-ab12] Verified by: 0xAAA at 1"
assert td._find_verifier_addr([jury], 0, "ab12") == "0xBBB"
assert td._find_verifier_addr([plain], 0, "ab12") == "0xAAA"

for contract, peer, secaas in [("lv", "3tx-peer", "2tx-secaas"), ("rr", "3tx-peer", "2tx-secaas"),
                               ("v2", "2tx-peer", "2tx-secaas"), ("v3", "jury", "secaas")]:
    td.CONTRACT = contract
    assert td.classify_verifier("0xBBB") == ("robot2", peer), contract
    assert td.classify_verifier("0xAAA") == ("secaas", secaas), contract
    assert td.classify_verifier("0xccc") == ("unknown", "unknown"), contract

for env, want in [("A=1\nCONTRACT=AttestationManagerV3\n", "v3"), ("CONTRACT=AttestationManagerV2", "v2"),
                  ("CONTRACT=Foo\n", SystemExit), ("A=1\n", SystemExit)]:
    with tempfile.TemporaryDirectory() as d:
        pathlib.Path(d, ".env").write_text(env)
        td.REPO_ROOT = pathlib.Path(d)
        try:
            got = td.detect_contract_variant()
        except SystemExit:
            got = SystemExit
        assert got == want, (env, got)

assert all(name in (ROOT / "start.sh").read_text() for name in td.CONTRACTS)
print("ok")
