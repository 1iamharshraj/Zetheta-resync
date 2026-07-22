#!/usr/bin/env bash
# Start the Zetheta resync web service with gunicorn.
# Intended to be run from the project root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Activate the virtual environment created by deploy-gcp.sh.
if [[ -f .venv/bin/activate ]]; then
    # shellcheck source=/dev/null
    source .venv/bin/activate
else
    echo "WARNING: .venv not found. Falling back to system python." >&2
fi

# Load environment variables from .env if present.
if [[ -f .env ]]; then
    set -a
    # shellcheck source=/dev/null
    source .env
    set +a
fi

export APP_CODE="${APP_CODE:-}"
export FLASK_PORT="${FLASK_PORT:-5000}"
export LOKI_URL="${LOKI_URL:-http://localhost:3100}"
export RUN_OUTPUT_DIR="${RUN_OUTPUT_DIR:-./runs}"
export WORKERS="${WORKERS:-1}"

mkdir -p "$RUN_OUTPUT_DIR"
mkdir -p logs

exec gunicorn \
    -b "0.0.0.0:${FLASK_PORT}" \
    -w "${WORKERS}" \
    --timeout 300 \
    --access-logfile logs/access.log \
    --error-logfile logs/error.log \
    --capture-output \
    --enable-stdio-inheritance \
    app:app
