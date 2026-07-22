#!/usr/bin/env bash
# Bootstrap a GCP Compute Engine VM for the Zetheta resync service.
# Run this script on the VM after creating it (e.g. via SSH).

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_FILE="scripts/resync-zetheta.service"
SERVICE_NAME="resync-zetheta"
USER="${SUDO_USER:-$USER}"
GROUP="$(id -gn "$USER" 2>/dev/null || echo "$USER")"

# Update system packages and install dependencies.
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv python3-full git docker.io docker-compose

# Stop any old standalone Loki/Grafana containers that may conflict with our ports.
for container in loki grafana; do
    if sudo docker ps -q -f "name=$container" | grep -q .; then
        echo "Stopping existing container: $container"
        sudo docker stop "$container"
        sudo docker rm "$container" || true
    fi
done

# Ensure the project directory is owned by the deploy user.
sudo chown -R "$USER:$GROUP" "$PROJECT_DIR" || true

cd "$PROJECT_DIR"

# Create Python virtual environment and install dependencies.
if [[ ! -d .venv ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Create .env if missing (copy from example). Edit manually if needed.
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "WARNING: .env was created from .env.example. Please edit $PROJECT_DIR/.env with the real APP_CODE."
fi

# Start Loki + Grafana via Docker Compose.
sudo docker compose up -d loki grafana

# Install and configure systemd service for the app.
# Generate a temporary service file with the correct user and project path.
sed -e "s|User=ubuntu|User=$USER|g" \
    -e "s|Group=ubuntu|Group=$GROUP|g" \
    -e "s|/opt/zetheta-resync|$PROJECT_DIR|g" \
    "$SERVICE_FILE" > /tmp/resync-zetheta.service

sudo cp /tmp/resync-zetheta.service /etc/systemd/system/$SERVICE_NAME.service
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME
sudo systemctl restart $SERVICE_NAME

echo "Deployment complete."

EXTERNAL_IP=$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null || echo 'VM_EXTERNAL_IP')
echo "App:     http://$EXTERNAL_IP:5000"
echo "Grafana: http://$EXTERNAL_IP:3000 (admin/admin)"
