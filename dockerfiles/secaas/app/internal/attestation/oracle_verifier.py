# app/internal/attestation/oracle_verifier.py

import os
import time
import threading
from queue import Queue, Empty
from typing import Optional, Dict, Set
from fastapi import FastAPI
from web3 import Web3
from app.internal.logger import info, warn, error, debug, green_text
from app.internal.blockchain.types import (
    AttestationState,
    AttestationManagerContractEvents
)
from app.internal.blockchain.event_watcher import EventWatcher
from app.internal.utils import helpers

def _should_stop(stop_event):
    return bool(stop_event and stop_event.is_set())

def process_secaas_attestation(
    app: FastAPI, 
    attestation_id: str, 
    seed_ts: Optional[Dict[str, int]] = None,
    stop_event: Optional[threading.Event] = None,
):
    try:
        # 1. Initialize State & Timestamps
        timestamps = dict(seed_ts or {})
        log_prefix = f"[SECaaS-{helpers.short_att_id(attestation_id)}]"

        blockchain_client = app.state.blockchain_client
        db_client = app.state.db_client
        should_wait = getattr(app.state, "wait_for_tx_confirmations", False)

        timestamps["oracle_start"] = helpers.now_ms()
        timestamps["p_oracle_start"] = helpers.perf_ns()

        # 2. Fetch Prover Metadata
        info(f"{log_prefix} Retrieving prover address from SC...")
        timestamps["get_prover_addr_start"] = helpers.now_ms()
        timestamps["p_get_prover_addr_start"] = helpers.perf_ns()

        agent_address = blockchain_client.get_prover_address(attestation_id)

        timestamps["p_get_prover_addr_finished"] = helpers.perf_ns()
        timestamps["get_prover_addr_finished"] = helpers.now_ms()

        info(f"{log_prefix} Retrieving prover ref. measurement from DB...")

        timestamps["get_prover_ref_signatures_db_start"] = helpers.now_ms()
        timestamps["p_get_prover_ref_signatures_db_start"] = helpers.perf_ns()

        signatures_dict = db_client.get_ref_signatures(agent_address)
        if not signatures_dict:
            error(f"{log_prefix} Agent '{agent_address}' not found in Database.")
            return
        
        timestamps["p_get_prover_ref_signatures_db_finished"] = helpers.perf_ns()
        timestamps["get_prover_ref_signatures_db_finished"] = helpers.now_ms()

        ref_hash = signatures_dict["combined_hash"]

        # 4. Check if SECaaS is the elected Verifier
        is_secaas_verifier = blockchain_client.is_verifier_agent(attestation_id)

        # Metadata
        timestamps["is_secaas_verifier"] = is_secaas_verifier

        if is_secaas_verifier:
            log_prefix = f"[Verifier-{helpers.short_att_id(attestation_id)}]"
            # --- ATOMIC RESOLUTION PATH ---
            info(f"{log_prefix} SECaaS is elected Verifier. Resolving atomically...")
            timestamps["verifier_start"] = timestamps["oracle_start"]
            timestamps["p_verifier_start"] = timestamps["p_oracle_start"]

            timestamps["get_signatures_start"] = helpers.now_ms()
            timestamps["p_get_signatures_start"] = helpers.perf_ns()

            fresh_sig_hex, chain_ref_hex, iter_count = blockchain_client.get_attestation_signatures(attestation_id)
            # V2 and V3 anchor the reference on chain: compare against it, so a
            # wrong or tampered SECaaS database cannot change the verdict.
            if blockchain_client.has_anchored_reference():
                ref_hash = chain_ref_hex

            timestamps["p_get_signatures_finished"] = helpers.perf_ns()
            timestamps["get_signatures_finished"] = helpers.now_ms()

            # 2. Compare Signatures
            timestamps["verify_compute_start"] = helpers.now_ms()
            timestamps["p_verify_compute_start"] = helpers.perf_ns()

            # Re-derive the chained reference under the no-tamper hypothesis:
            # all K m_i = ref_hash, so prover's H_K equals chain(ref_hash, K).
            # K=1 is the identity case. Construction must stay bit-for-bit
            # identical to compute.RollingHashStep used in the Go prover.
            try:
                derived_ref = helpers.rolling_hash(ref_hash, int(iter_count))
            except Exception as ex:
                error(f"{log_prefix} Failed to derive rolling reference (K={iter_count}): {ex}")
                derived_ref = ref_hash

            # Web3.to_hex prepends "0x"; derived_ref is bare hex.
            def _strip0x(s: str) -> str:
                return s[2:] if s.lower().startswith("0x") else s
            is_success = (_strip0x(fresh_sig_hex).lower() == _strip0x(derived_ref).lower())
            # is_success = True

            timestamps["p_verify_compute_finished"] = helpers.perf_ns()
            timestamps["verify_compute_finished"] = helpers.now_ms()

            # 3. Close Attestation
            info(f"{log_prefix} Resolved atomically (result: {'✅ SUCCESS' if is_success else '❌ FAILURE'})")
            
            # Metadata
            timestamps["verification_result"] = is_success

            timestamps["result_sent"] = helpers.now_ms()
            timestamps["p_send_result_start"] = helpers.perf_ns()
            # Retry resolve_attestation once on transient failure (e.g. nonce race,
            # RPC timeout).  A second failure is a real error; let the outer
            # except handle it so the worker moves on rather than blocking.
            for _attempt in range(2):
                try:
                    blockchain_client.resolve_attestation(attestation_id, is_success, wait=should_wait)
                    break
                except Exception as resolve_err:
                    if _attempt == 0:
                        warn(f"{log_prefix} resolve_attestation failed (attempt 1), retrying: {resolve_err}")
                        time.sleep(0.5)
                    else:
                        raise
            timestamps["p_send_result_finished"] = helpers.perf_ns()
            if should_wait:
                timestamps["p_send_result_finished_tx_confirmed"] = helpers.perf_ns()
                timestamps["result_sent_tx_confirmed"] = helpers.now_ms()

            timestamps["p_verifier_finished"] = helpers.perf_ns()
            timestamps["verifier_finished"] = helpers.now_ms()

        else:
            log_prefix = f"[Oracle-{helpers.short_att_id(attestation_id)}]"
            info(f"{log_prefix} Prover ref. measurement sent to the smart contract")
            timestamps["prover_ref_signature_sent"] = helpers.now_ms()
            timestamps["p_send_prover_ref_signature_start"] = helpers.perf_ns()
            tx_hash = blockchain_client.send_reference_signature(
                attestation_id, 
                signatures_dict["combined_hash"], 
                wait=should_wait 
            )
            timestamps["p_send_prover_ref_signature_finished"] = helpers.perf_ns()
            if should_wait:
                timestamps["p_send_prover_ref_signature_finished_tx_confirmed"] = helpers.perf_ns()
                timestamps["prover_ref_signature_sent_tx_confirmed"] = helpers.now_ms()

            timestamps["p_oracle_finished"] = helpers.perf_ns()
            timestamps["oracle_finished"] = helpers.now_ms()

        if app.state.export_enabled:
            helpers.export_attestation_times_json(
                app.state.participant_name, 
                attestation_id, 
                "oracle", 
                timestamps, 
                app.state.results_dir,
                json_path=getattr(app.state, "results_file", None)
            )
            if is_secaas_verifier:
                helpers.export_attestation_times_json(
                    app.state.participant_name, 
                    attestation_id, 
                    "verifier", 
                    timestamps, 
                    app.state.results_dir,
                    json_path=getattr(app.state, "results_file", None)
                )

    except Exception as e:
        error(f"Error while processing attestation '{attestation_id}': {e}")
        import traceback
        debug(traceback.format_exc())
        
def run_expiry_logic(app: FastAPI, stop_event: Optional[threading.Event] = None):
    """V3: expire the jury cycles still waiting for votes after the jury timeout.
    The prover expires its own cycle too; this covers a prover that went down.
    The expired cycle then reaches the SECaaS through AttestationStarted."""
    blockchain_client = app.state.blockchain_client
    evt_enum = AttestationManagerContractEvents.READY_FOR_EVALUATION
    watcher = EventWatcher(
        web3            = blockchain_client.web3,
        contract        = blockchain_client.contract,
        event_abi       = blockchain_client.get_event_abi(evt_enum),
        address         = blockchain_client.contract.address,
        topics          = [blockchain_client.get_event_topic(evt_enum)],
        checkpoint_path = os.path.join(app.state.event_checkpoint_dir, "ready_cp_secaas.json"),
        confirmations   = app.state.event_confirmations,
        batch_size      = app.state.event_batch_size,
        poll_interval   = app.state.event_poll_interval,
    )
    timeout_blocks = blockchain_client.jury_timeout_blocks()
    pending: Dict[str, int] = {}  # attestation id -> block of its evidence
    lock = threading.Lock()

    def handle(evt):
        attestation_id = Web3.to_text(evt['args']['id']).rstrip('\x00').strip()
        with lock:
            pending[attestation_id] = evt['blockNumber']

    def expire_due():
        while not _should_stop(stop_event):
            head = blockchain_client.web3.eth.block_number
            with lock:
                # Gas estimation runs at the head block, so wait for head = evidence + timeout.
                due = [i for i, b in pending.items() if head >= b + timeout_blocks]
            for attestation_id in due:
                log_prefix = f"[SECaaS-{helpers.short_att_id(attestation_id)}]"
                if blockchain_client.get_attestation_state(attestation_id) == AttestationState.ReadyForEvaluation:
                    try:
                        blockchain_client.expire(attestation_id, wait=True)
                        info(f"{log_prefix} Jury timeout reached, cycle {attestation_id} expired by the SECaaS")
                    except Exception as e:
                        # The jury or the prover may have acted first: the state is read again next round.
                        warn(f"{log_prefix} Expire not sent: {e}")
                        continue
                with lock:
                    pending.pop(attestation_id, None)
            time.sleep(app.state.event_poll_interval)

    t = threading.Thread(target=expire_due, name="SECaaS-expiry", daemon=True)
    t.start()
    info(f"Expiring jury cycles left without a verdict after {timeout_blocks} blocks")
    watcher.run(handle, stop_event=stop_event)
    t.join(timeout=30)

def run_secaas_logic(app: FastAPI, stop_event: Optional[threading.Event] = None):
    # Build blockchain_event_watcher for AttestationStarted
    evt_enum = AttestationManagerContractEvents.ATTESTATION_STARTED
    blockchain_client = app.state.blockchain_client
    evt_abi  = blockchain_client.get_event_abi(evt_enum)
    topic0   = blockchain_client.get_event_topic(evt_enum)

    cp_name  = f"att_started_cp_secaas.json"
    cp_path  = os.path.join(app.state.event_checkpoint_dir, cp_name)

    blockchain_event_watcher = EventWatcher(
        web3            = blockchain_client.web3,
        contract        = blockchain_client.contract,
        event_abi       = evt_abi,
        address         = blockchain_client.contract.address,
        topics          = [topic0],
        checkpoint_path = cp_path,
        confirmations   = app.state.event_confirmations,
        batch_size      = app.state.event_batch_size,
        poll_interval   = app.state.event_poll_interval,
    )

    # Initial lookback (when no checkpoint)
    if not os.path.exists(cp_path):
        latest = blockchain_client.web3.eth.block_number
        if app.state.event_lookback_blocks > 0:
            blockchain_event_watcher.from_block = max(0, latest - app.state.event_lookback_blocks)
        else:
            # small safe overlap even when lookback is 0
            blockchain_event_watcher.from_block = max(0, latest - app.state.event_confirmations)
        info(f"Initial lookback set to block {blockchain_event_watcher.from_block} (latest={latest})")

    info("Subscribed to attestation events with EventWatcher...")

    work_q = Queue()
    enqueued_ids: Set[str] = set()
    enq_lock = threading.Lock()

    def worker():
        # Exit only when BOTH: stop is set AND the queue is empty.
        # This guarantees every in-flight / already-queued item is processed and
        # task_done() is called before the thread exits, so work_q.join() in
        # stop_attestation never deadlocks and all exports finish before
        # mark_experiment_stop sets Stopped=true.
        while True:
            try:
                attestation_id, seed_ts = work_q.get(timeout=0.05)
            except Empty:
                if _should_stop(stop_event):
                    break  # queue empty + stop requested → exit cleanly
                continue
            try:
                info(f"Processing attestation '{attestation_id}'")
                process_secaas_attestation(app, attestation_id, seed_ts, stop_event=stop_event)
            except Exception as e:
                error(f"Error processing {attestation_id}: {e}")
            finally:
                with enq_lock:
                    enqueued_ids.discard(attestation_id)
                work_q.task_done()

        info("SECaaS worker stopped")

    t_worker = threading.Thread(target=worker, name="SECaaS", daemon=True)
    t_worker.start()
    if hasattr(app.state, "threads"):
        app.state.threads.append(t_worker)

    def handle(evt):
        arrival_ts = helpers.now_ms()
        arrival_p  = helpers.perf_ns()

        attestation_id = Web3.to_text(evt['args']['id']).rstrip('\x00').strip()
        
        # if blockchain_client.get_attestation_state(attestation_id) == AttestationState.Open:
        #    with enq_lock:
        #         if attestation_id not in enqueued_ids:
        #             enqueued_ids.add(attestation_id)
        #             seed_ts = {
        #                 "attestation_started_received": arrival_ts,
        #                 "p_attestation_started_received": arrival_p
        #             }
        #             work_q.put((attestation_id, seed_ts))

        with enq_lock:
            if attestation_id not in enqueued_ids:
                enqueued_ids.add(attestation_id)
                seed_ts = {
                    "attestation_started_received": arrival_ts,
                    "p_attestation_started_received": arrival_p
                }
                work_q.put((attestation_id, seed_ts))

    blockchain_event_watcher.run(handle, stop_event=stop_event)
    # After watcher exits, signal worker to stop and drain.
    # Worker exits only when queue is empty + stop set, so joining here
    # guarantees all exports are written before run_secaas_logic returns.
    if stop_event:
        stop_event.set()
    t_worker.join(timeout=30)
    info("SECaaS logic stopped")