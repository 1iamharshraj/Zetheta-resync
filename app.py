#!/usr/bin/env python3
"""
Flask web service for the Zetheta resync job.

Endpoints:
  POST /api/v1/run     Trigger a resync (type: all|tech|nontech, limit, dry_run, output)
  GET  /api/v1/status  Get the current / last job status
  GET  /api/v1/health  Health check
  GET  /               Basic UI / links
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request

from loki_config import setup_logging
from resync import get_app_code as resync_get_app_code, load_dotenv, run_resync

# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------
load_dotenv(".env")

APP_CODE = os.environ.get("APP_CODE", "").strip()
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))
LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")
RUN_OUTPUT_DIR = os.environ.get("RUN_OUTPUT_DIR", "./runs")
RESYNC_WORKERS = int(os.environ.get("RESYNC_WORKERS", "3"))
OUTPUT_RETENTION_DAYS = int(os.environ.get("OUTPUT_RETENTION_DAYS", "3"))

if not os.path.isdir(RUN_OUTPUT_DIR):
    os.makedirs(RUN_OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
setup_logging(level=logging.INFO, loki_url=LOKI_URL, labels={"job": "app-logs", "service": "zetheta-resync"})

# ---------------------------------------------------------------------------
# Flask app + job tracker
# ---------------------------------------------------------------------------
app = Flask(__name__)

jobs: Dict[str, Dict[str, Any]] = {}
current_job_id: Optional[str] = None
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def cleanup_old_outputs() -> None:
    """Delete CSV output files older than OUTPUT_RETENTION_DAYS to save disk."""
    cutoff = time.time() - (OUTPUT_RETENTION_DAYS * 24 * 60 * 60)
    removed = 0
    for fname in os.listdir(RUN_OUTPUT_DIR):
        fpath = os.path.join(RUN_OUTPUT_DIR, fname)
        if os.path.isfile(fpath) and fname.endswith(".csv") and os.path.getmtime(fpath) < cutoff:
            try:
                os.remove(fpath)
                removed += 1
            except OSError as exc:
                logging.warning("[CLEANUP] Could not remove %s: %s", fpath, exc)
    if removed:
        logging.info("[CLEANUP] Removed %d old output CSV(s)", removed)


def get_app_code() -> str:
    """Return app code from env; raise if missing."""
    code = APP_CODE or resync_get_app_code(type("Args", (), {"app_code": "", "env_file": ".env"})())
    if not code:
        raise ValueError("APP_CODE is not configured")
    return code


def run_job(job_id: str, config: Dict[str, Any]) -> None:
    """Background worker that runs the resync job."""
    global current_job_id
    logging.info("[JOB] Starting job %s with config %s", job_id, config)

    with _lock:
        jobs[job_id] = {
            "job_id": job_id,
            "status": "running",
            "config": config,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "result": None,
            "error": None,
        }
        current_job_id = job_id

    try:
        result = run_resync(config)
        with _lock:
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            jobs[job_id]["result"] = result
    except Exception as exc:  # noqa: BLE001
        logging.exception("[JOB] Job %s failed: %s", job_id, exc)
        with _lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            jobs[job_id]["error"] = str(exc)
    finally:
        with _lock:
            if current_job_id == job_id:
                current_job_id = None
        cleanup_old_outputs()
        logging.info("[JOB] Job %s finished", job_id)


def build_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build a resync config from an API request payload."""
    run_type = payload.get("type", "all")
    if run_type not in ("all", "tech", "nontech"):
        raise ValueError("type must be one of: all, tech, nontech")

    limit = int(payload.get("limit", 0))
    if limit < 0:
        raise ValueError("limit must be >= 0")

    workers = int(payload.get("workers", RESYNC_WORKERS))
    if workers < 1:
        raise ValueError("workers must be >= 1")

    dry_run = bool(payload.get("dry_run", False))
    output = payload.get("output", "")
    if output:
        # Ensure output path is inside the runs directory.
        output = os.path.basename(output)
        output = os.path.join(RUN_OUTPUT_DIR, output)

    user_ids = payload.get("user_ids", "")
    if isinstance(user_ids, list):
        user_ids = ",".join(str(uid) for uid in user_ids)

    return {
        "app_code": get_app_code(),
        "type": run_type,
        "limit": limit,
        "workers": workers,
        "dry_run": dry_run,
        "output": output,
        "verbose": False,
        "user_ids": user_ids,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index() -> Any:
    return jsonify({
        "service": "zetheta-resync",
        "endpoints": {
            "run": "POST /api/v1/run",
            "status": "GET /api/v1/status",
            "health": "GET /api/v1/health",
        },
    })


@app.route("/api/v1/health", methods=["GET"])
def health() -> Any:
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/api/v1/status", methods=["GET"])
def status() -> Any:
    with _lock:
        last_job = None
        if jobs:
            last_job_id = list(jobs.keys())[-1]
            last_job = jobs[last_job_id]
        return jsonify({
            "current_job_id": current_job_id,
            "last_job": last_job,
            "total_jobs": len(jobs),
        })


@app.route("/api/v1/run", methods=["POST"])
def run() -> Any:
    payload = request.get_json(silent=True) or {}

    try:
        config = build_config(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = f"job-{uuid.uuid4().hex[:8]}"
    config["job_id"] = job_id

    thread = threading.Thread(target=run_job, args=(job_id, config), daemon=True)
    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "started",
        "config": config,
    })


@app.route("/api/v1/jobs/<job_id>", methods=["GET"])
def job_detail(job_id: str) -> Any:
    with _lock:
        if job_id not in jobs:
            return jsonify({"error": "job not found"}), 404
        return jsonify(jobs[job_id])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
