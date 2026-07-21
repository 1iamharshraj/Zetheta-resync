#!/usr/bin/env bash
# Quick validation suite for the Zetheta resync CLI.
# This script does NOT perform real API updates; it checks syntax,
# help output, connectivity, and a small dry-run sample.

set -euo pipefail

echo "=== 1. Syntax check ==="
python3 -m py_compile resync.py

echo "=== 2. Help output ==="
python3 resync.py --help | head -n 20

echo "=== 3. Connectivity / sample report test ==="
python3 resync.py --test || true

echo "=== 4. Small dry-run with explicit app code ==="
python3 resync.py --app-code DUMMY --type nontech --limit 3 --dry-run --verbose

echo "=== All checks completed ==="
