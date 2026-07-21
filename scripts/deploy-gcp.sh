#!/usr/bin/env bash
# Bootstrap a GCP Compute Engine VM for the Zetheta resync service.
# Run this script on the VM after creating it (e.g. via SSH).

set -euo pipefail

PROJECT_DIR="/opt/zetheta-resync"
SERVICE_FILE="scripts/resync-zetheta.service"

# Update system packages and install dependencies.
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv git docker.io docker-compose

# Clone or pull the repo.
if [[ -d "$PROJECT_DIR/.git" ]]; then
    cd "$PROJECT_DIR"
    sudo -H -u ubuntu git pull
else
    sudo mkdir -p "$PROJECT_DIR"
    sudo chown ubuntu:ubuntu "$PROJECT_DIR"
    # Replace the URL below with your actual repository URL.
    sudo -H -u ubuntu git clone https://github.com/YOUR_ORG/zetheta-resync.git "$PROJECT_DIR"
fi

cd "$PROJECT_DIR"

# Install Python dependencies.
sudo -H -u ubuntu python3 -m pip install --user -r requirements.txt

# Create .env if missing (copy from example). Edit manually if needed.
if [[ ! -f .env ]]; then
    sudo -H -u ubuntu cp .env.example .env
    echo "WARNING: .env was created from .env.example. Please edit $PROJECT_DIR/.env with the real APP_CODE."
fi

# Start Loki + Grafana via Docker Compose.
sudo docker compose up -d loki grafana

# Install systemd service for the app.
sudo cp "$SERVICE_FILE" /etc/systemd/system/resync-zetheta.service
sudo systemctl daemon-reload
sudo systemctl enable resync-zetheta
sudo systemctl restart resync-zetheta

echo "Deployment complete."
echo "App:     http://$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || echo 'VM_IP'):5000"
echo "Grafana: http://$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || echo 'VM_IP'):3000 (admin/admin)"
