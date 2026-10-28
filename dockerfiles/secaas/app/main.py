import os
import re
import glob
import json
import threading

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.internal.logger import init_logging, info, warn, error, debug, green_text
from app.internal.utils import helpers
from app.internal.blockchain.client import BlockchainClient
from app.internal.attestation.oracle_verifier import run_secaas_logic, run_expiry_logic
from app.internal.database.client import DatabaseClient

# -----------------------------------------------------------------------------
# Logging init (custom logger)
# -----------------------------------------------------------------------------
init_logging()

app = FastAPI()

_config_lock = threading.Lock()

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _next_run_json(results_dir: str, participant: str) -> str:
    os.makedirs(results_dir, exist_ok=True)
    base = results_dir.rstrip("/")
    safe = participant  # names already controlled; adjust if needed
    pattern = os.path.join(base, f"{safe}-run*.json")
    existing = glob.glob(pattern)
    max_idx = 0
    rx = re.compile(rf"{re.escape(safe)}-run(\d+)\.json$")
    for p in existing:
        m = rx.search(os.path.basename(p))
        if m:
            try:
                idx = int(m.group(1))
                if idx > max_idx:
                    max_idx = idx
            except ValueError:
                pass
    return os.path.join(base, f"{safe}-run{max_idx+1}.json")

def start_attestation(app: FastAPI):
    # Create stop event / threads list if not present (idempotent)
    if not hasattr(app.state, "stop_event"):
        app.state.stop_event = threading.Event()
    if not hasattr(app.state, "threads"):
        app.state.threads = []

    # If we’re restarting, make sure stop is cleared
    app.state.stop_event.clear()

    if app.state.export_enabled:
        # Ensure per-run JSON path is set, then init file
        if not getattr(app.state, "results_file", None):
            # Fallback only (should rarely happen now)
            app.state.results_file = _next_run_json(app.state.results_dir, app.state.participant_name)
            info(f"Results file selected (new run): {app.state.results_file}")

        helpers.ensure_results_initialized(
            app.state.results_dir,
            app.state.participant_name,
            json_path=app.state.results_file
        )

    info("SECaaS attestation loop started")
    t = threading.Thread(target=run_secaas_logic, args=(app, app.state.stop_event), name="SECaaS", daemon=True)
    t.start()
    app.state.threads.append(t)
    if app.state.blockchain_client.has_jury():
        t = threading.Thread(target=run_expiry_logic, args=(app, app.state.stop_event), name="SECaaS-expiry-watch", daemon=True)
        t.start()
        app.state.threads.append(t)

def stop_attestation(app: FastAPI):

    # Signal workers to stop
    if hasattr(app.state, "stop_event"):
        app.state.stop_event.set()

    # Best-effort join of all tracked threads.
    # run_secaas_logic internally drains work_q before returning, so this
    # join guarantees all exports are written before mark_experiment_stop fires.
    for t in getattr(app.state, "threads", []):
        try:
            t.join(timeout=30)
        except Exception as e:
            warn(f"Failed joining thread {t.name}: {e}")

    # Reset the thread list after stopping
    if hasattr(app.state, "threads"):
        app.state.threads.clear()

    if app.state.export_enabled:
        helpers.mark_experiment_stop(
            app.state.results_dir, 
            app.state.participant_name, 
            json_path=getattr(app.state, "results_file", None)
        )
        # Reset filename so next start() calculates a new one
        app.state.results_file = None


# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    config_path = helpers.get_env_str("CONFIG_PATH", default="")
    if not config_path:
        error("CONFIG_PATH environment variable is required.")
        raise SystemExit(1)

    try:
        db_host = os.getenv("DB_HOST", "attestation-db")
        db_port = int(os.getenv("DB_PORT", "5432"))
        db_name = os.getenv("DB_NAME", "attestation")
        db_user = os.getenv("DB_USER", "postgres")
        db_pass = os.getenv("DB_PASSWORD", "postgres")

        app.state.db_client = DatabaseClient(
            host=db_host,
            port=db_port,
            database=db_name,
            user=db_user,
            password=db_pass
        )
        info(f"Database client initialized at {db_host}:{db_port}")
    except Exception as e:
        error(f"Failed to initialize Database: {e}")
        raise SystemExit(1)

    try:
        with open(config_path, "r") as json_file:
            data = json.load(json_file)

        required_fields = ["eth_address", "private_key", "eth_node_url", "contract_address"]
        missing = [f for f in required_fields if f not in data]
        if missing:
            error(f"Missing required config fields: {', '.join(missing)}")
            raise SystemExit(1)

        eth_address = data["eth_address"]
        private_key = data["private_key"]
        eth_node_url = data["eth_node_url"]
        contract_address = data["contract_address"]

    except SystemExit:
        raise
    except Exception as e:
        error(f"Configuration error: {str(e)}")
        raise SystemExit(1)

    # App state
    app.state.participant = "secaas"
    app.state.participant_name = data.get("name", "secaas")
    app.state.export_enabled = os.getenv("EXPORT_RESULTS", "false").lower() == "true"
    app.state.results_dir = helpers.get_env_str("RESULTS_DIR", default="/experiments/data/attestation-times")
    contract_abi = data.get("contract_abi")

    if not contract_abi:
        error("Missing 'contract_abi' in config JSON.")
        raise SystemExit(1)

    # Blockchain client
    app.state.blockchain_client = BlockchainClient(
        eth_address=eth_address,
        private_key=private_key,
        eth_node_url=eth_node_url,
        contract_address=contract_address,
        contract_abi=contract_abi,
    )

    app.state.wait_for_tx_confirmations = os.getenv("WAIT_FOR_TX_CONFIRMATIONS", "false").lower() == "true"

    app.state.active_attestations = {}   # attestation_id → Thread

    app.state.fail_attestation_flag = os.getenv("FAIL_ATTESTATION", "false").lower() == "true"

    # Events watcher config
    app.state.event_confirmations = helpers.get_env_int("EVENT_CONFIRMATIONS", default=1)
    app.state.event_batch_size = helpers.get_env_int("EVENT_BATCH_SIZE", default=1000)
    app.state.event_poll_interval = helpers.get_env_float("EVENT_POLL_INTERVAL", default=1)
    app.state.event_lookback_blocks = helpers.get_env_int("EVENT_LOOKBACK_BLOCKS", default=0)
    app.state.event_checkpoint_dir = helpers.get_env_str("EVENT_CHECKPOINT_DIR", default="/checkpoints")

    # Thread tracking
    app.state.stop_event = threading.Event()
    app.state.threads = []

    auto_start = os.getenv("AUTO_START", "false").lower() == "true"

    # Precompute results file
    app.state.results_file = None
    if app.state.export_enabled:
        run_id = os.getenv("RUN_ID", "").strip()
        if run_id:
            app.state.results_file = os.path.join(
                app.state.results_dir,
                f"{app.state.participant_name}-run{run_id}.json"
            )
            info(f"Preselected results file (RUN_ID={run_id}): {app.state.results_file}")
        else:
            app.state.results_file = _next_run_json(app.state.results_dir, app.state.participant_name)
            info(f"Preselected results file (auto): {app.state.results_file}")

    if auto_start:
        start_attestation(app)

    info(green_text("Startup completed for participant '%s'"), app.state.participant_name)


# -----------------------------------------------------------------------------
# API routes
# -----------------------------------------------------------------------------
@app.get("/")
def root_endpoint():
    return {"message": "SECaaS running."}

@app.post("/start")
def start_endpoint():
    start_attestation(app)
    return {"message": "Attestation started."}

@app.post("/stop")
def stop_endpoint():
    stop_attestation(app)
    return {"message": "Attestation stopped."}
    
@app.post("/reset")
def reset_attestation_chain():
    try:
        blockchain_client = app.state.blockchain_client
        tx_hash = blockchain_client.reset_attestation_chain(wait=True, timeout=30)
        return {"message": "Chain reset", "tx_hash": tx_hash}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/secaas-only")
async def set_secaas_only(enabled: bool):
    """
    Toggle secaasOnly mode on the contract.
    POST /secaas-only?enabled=true  → SECaaS is sole verifier (startup mode)
    POST /secaas-only?enabled=false → normal RR verifier election (mutual attestation)
    ResetChain() sets this to true automatically; call with enabled=false to enable mutual attestation.
    """
    try:
        tx_hash = app.state.blockchain_client.set_secaas_only(enabled, wait=True, timeout=30)
        return {"secaas_only": enabled, "tx_hash": tx_hash}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/config")
async def config_endpoint(body: dict):
    """Partial runtime config update. Accepted fields: results_dir, results_file, export_enabled.
    Rejected while an attestation is in progress.
    results_dir change also clears results_file so the next /start picks a fresh run number.
    results_file, if set, pins the output path for the next run (cleared automatically after the run).
    """
    results_dir    = body.get("results_dir")
    results_file   = body.get("results_file")
    export_enabled = body.get("export_enabled")

    if results_dir is None and results_file is None and export_enabled is None:
        return JSONResponse(status_code=400, content={"error": "no recognized fields: use results_dir, results_file, export_enabled"})

    with _config_lock:
        running = any(t.is_alive() for t in getattr(app.state, "threads", []))
        if running:
            return JSONResponse(status_code=409, content={"error": "cannot change config while attestation is running"})

        applied = {}

        if results_dir is not None:
            if not results_dir:
                return JSONResponse(status_code=400, content={"error": "results_dir must not be empty"})
            app.state.results_dir = results_dir
            app.state.results_file = None  # force fresh run-number selection on next /start
            applied["results_dir"] = results_dir
            info(f"Results dir set to: {results_dir}")

        if results_file is not None:
            app.state.results_file = results_file or None
            applied["results_file"] = results_file
            info(f"Results file set to: {results_file}")

        if export_enabled is not None:
            app.state.export_enabled = bool(export_enabled)
            applied["export_enabled"] = bool(export_enabled)
            info(f"Export enabled set to: {export_enabled}")

    return {"applied": applied}

@app.post("/sync-agents")
async def sync_agents_endpoint(n_robots: int = 0):
    """
    Scans the config directory for robot*.json files and
    updates the database with their reference signatures.
    If n_robots > 0, only robot1.json .. robotN.json are processed.
    Results are always returned in ascending numeric order.
    """
    config_dir = "/ref-measurements"
    pattern = os.path.join(config_dir, "robot*.json")
    all_files = glob.glob(pattern)

    # Extract numeric index from filename and sort
    def robot_index(path: str):
        m = re.search(r"robot(\d+)\.json$", os.path.basename(path))
        return int(m.group(1)) if m else -1

    all_files.sort(key=robot_index)

    # Filter to the active robot set when n_robots is specified
    if n_robots > 0:
        config_files = [f for f in all_files if 1 <= robot_index(f) <= n_robots]
    else:
        config_files = all_files
    
    if not config_files:
        return JSONResponse(
            status_code=404, 
            content={"message": "No robot configuration files found."}
        )

    results = []
    success_count = 0

    for file_path in config_files:
        try:
            with open(file_path, "r") as f:
                data = json.load(f)

            addr = data.get("eth_address")
            name = data.get("name", os.path.basename(file_path))

            robot_hash = data.get("robot_hash")
            attestation_sidecar_hash = data.get("attestation_sidecar_hash")
            combined_hash = data.get("combined_hash")

            if addr and attestation_sidecar_hash and combined_hash and robot_hash:
                success = app.state.db_client.add_ref_signatures(
                    addr,
                    robot_hash,
                    attestation_sidecar_hash,
                    combined_hash
                )
                if success:
                    success_count += 1
                    results.append({"agent": name, "status": "synced", "address": addr})
                else:
                    results.append({"agent": name, "status": "failed", "address": addr})
            else:
                missing = []
                if not addr: missing.append("eth_address")
                if not attestation_sidecar_hash: missing.append("attestation_sidecar_hash")
                if not combined_hash: missing.append("combined_hash")
                if not robot_hash: missing.append("robot_hash")

                results.append({
                    "agent": name,
                    "status": "skipped",
                    "reason": "missing data",
                    "missing": missing,
                })

        except Exception as e:
            error(f"Error syncing {file_path}: {e}")
            results.append({"file": file_path, "status": "error", "error": str(e)})

    return {
        "message": f"Sync completed. {success_count}/{len(config_files)} agents updated.",
        "details": results,
    }
    
# -----------------------------------------------------------------------------
# Shutdown
# -----------------------------------------------------------------------------
@app.on_event("shutdown")
async def shutdown():
    stop_attestation(app)

    if hasattr(app.state, "blockchain_client"):
        getattr(app.state.blockchain_client, "close", lambda: None)()
    
    if hasattr(app.state, "db_client"):
        app.state.db_client.close()
        info("Database connection closed during shutdown.")