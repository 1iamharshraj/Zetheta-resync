#!/usr/bin/env bash
set -euo pipefail
sudo systemctl restart resync-zetheta
sudo systemctl status resync-zetheta --no-pager
