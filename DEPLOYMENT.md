# Deployment Guide

This guide covers running the Zetheta resync service locally with Docker Compose and on a GCP Compute Engine VM with systemd + gunicorn.

## Local development with Docker Compose

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env and set APP_CODE to the real value.
```

### 2. Start the stack

```bash
docker compose up --build -d
```

This starts:
- **App** on `http://localhost:5000`
- **Loki** on `http://localhost:3100`
- **Grafana** on `http://localhost:3000` (login: `admin` / `admin`)

### 3. Trigger a resync

```bash
# Run everything (tech + non-tech)
curl -X POST http://localhost:5000/api/v1/run \
  -H "Content-Type: application/json" \
  -d '{"type": "all"}'

# Run only tech
curl -X POST http://localhost:5000/api/v1/run \
  -H "Content-Type: application/json" \
  -d '{"type": "tech"}'

# Run only non-tech
curl -X POST http://localhost:5000/api/v1/run \
  -H "Content-Type: application/json" \
  -d '{"type": "nontech"}'

# Run with a limit and dry-run mode
curl -X POST http://localhost:5000/api/v1/run \
  -H "Content-Type: application/json" \
  -d '{"type": "tech", "limit": 100, "dry_run": true}'
```

### 4. Check status and logs

```bash
# Service health
curl http://localhost:5000/api/v1/health

# Current / last job status
curl http://localhost:5000/api/v1/status

# Specific job details
curl http://localhost:5000/api/v1/jobs/<job_id>
```

Open Grafana at `http://localhost:3000` → **Dashboards** → **Zetheta Resync**.

### 5. Stop the stack

```bash
docker compose down
```

To remove volumes (Loki/Grafana data):

```bash
docker compose down -v
```

---

## GCP Compute Engine deployment

### 1. Create a VM

Use the GCP Console or gcloud:

```bash
gcloud compute instances create zetheta-resync \
  --machine-type=e2-medium \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB \
  --tags=http-server,https-server \
  --zone=asia-south1-a
```

Allow HTTP/HTTPS traffic in the firewall rules.

### 2. SSH into the VM and run the bootstrap

```bash
gcloud compute ssh zetheta-resync --zone=asia-south1-a
```

Inside the VM:

```bash
# Install git if not already present
sudo apt-get update && sudo apt-get install -y git

# Clone the repository
git clone https://github.com/YOUR_ORG/zetheta-resync.git /opt/zetheta-resync

cd /opt/zetheta-resync

# Run the bootstrap script
sudo bash scripts/deploy-gcp.sh
```

The script:
- installs Python, pip, Docker, Docker Compose
- installs the app dependencies
- starts Loki + Grafana in Docker
- installs and starts the systemd service for the Flask app

### 3. Edit .env with the real APP_CODE

```bash
sudo nano /opt/zetheta-resync/.env
```

Set:

```ini
APP_CODE=YOUR_REAL_APP_CODE
LOKI_URL=http://localhost:3100
FLASK_PORT=5000
```

Restart the service:

```bash
sudo systemctl restart resync-zetheta
```

### 4. Access the services

Use the VM's external IP:

- App: `http://<VM_EXTERNAL_IP>:5000`
- Grafana: `http://<VM_EXTERNAL_IP>:3000` (admin/admin)

### 5. Manage the service

```bash
# Status
sudo systemctl status resync-zetheta

# Restart
sudo bash /opt/zetheta-resync/scripts/restart.sh

# Stop
sudo bash /opt/zetheta-resync/scripts/stop.sh

# View logs
sudo journalctl -u resync-zetheta -f
```

### 6. Trigger resyncs on GCP

```bash
# Tech only
curl -X POST http://<VM_EXTERNAL_IP>:5000/api/v1/run \
  -H "Content-Type: application/json" \
  -d '{"type": "tech"}'

# Non-tech only
curl -X POST http://<VM_EXTERNAL_IP>:5000/api/v1/run \
  -H "Content-Type: application/json" \
  -d '{"type": "nontech"}'

# Everything
curl -X POST http://<VM_EXTERNAL_IP>:5000/api/v1/run \
  -H "Content-Type: application/json" \
  -d '{"type": "all"}'
```

---

## Loki log retention

Loki is configured to retain logs for **7 days**. Older logs are automatically deleted by the compactor.

To change retention, edit `loki/loki-config.yaml` and adjust:

```yaml
limits_config:
  retention_period: 168h  # 7 days

table_manager:
  retention_deletes_enabled: true
  retention_period: 168h
```

Then restart the Loki container:

```bash
docker compose restart loki
```

---

## Troubleshooting

### App logs show "[LOKI] Failed to push ... Connection refused"

The app tries to push logs to Loki. If Loki is not running or the URL is wrong, logs still go to stdout and files. Make sure `LOKI_URL` is correct:

- Local Docker: `http://loki:3100`
- Local bare metal: `http://localhost:3100`
- GCP VM: `http://localhost:3100` (Loki runs in Docker with port 3100 mapped)

### Grafana shows "No data"

1. Check that Loki is healthy: `curl http://localhost:3100/ready`
2. Check the datasource config in Grafana: **Configuration** → **Data sources** → **Loki**
3. Trigger a resync so logs are generated.

### systemd service fails to start

```bash
sudo journalctl -u resync-zetheta -n 50
```

Common issues:
- `.env` not present or missing `APP_CODE`
- `gunicorn` not installed: run `python3 -m pip install --user -r requirements.txt`
- Permission issues: make sure the service runs as the user that owns `/opt/zetheta-resync`

### API returns HTML instead of JSON

Make sure the request URL uses `www.zetheta.com` for the update endpoint. The script is configured with `https://www.zetheta.com/wp-json/v1/update_submissions/`.
