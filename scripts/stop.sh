#!/usr/bin/env bash
set -euo pipefail
sudo systemctl stop resync-zetheta
sudo systemctl status resync-zetheta --no-pager
