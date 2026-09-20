#!/usr/bin/env bash
# Helper to install collector as a systemd service on a lab Linux PC.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${PREFIX:-/opt/tlmtry}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

id tlmtry &>/dev/null || useradd --system --home /var/lib/tlmtry --shell /usr/sbin/nologin tlmtry
usermod -aG dialout tlmtry || true

mkdir -p /var/lib/tlmtry /etc/tlmtry "$PREFIX"
rsync -a --delete "$ROOT/" "$PREFIX/" --exclude .git --exclude '**/__pycache__' --exclude .venv

python3 -m pip install -e "$PREFIX/collector"
# For PostgreSQL primary: python3 -m pip install -e "$PREFIX/collector[postgres]"
if [[ ! -f /etc/tlmtry/collector.yaml ]]; then
  cp "$PREFIX/config/collector.example.yaml" /etc/tlmtry/collector.yaml
  echo "Created /etc/tlmtry/collector.yaml — edit serial port (and database) before starting."
fi

cp "$PREFIX/deploy/tlmtry-collector.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable tlmtry-collector
echo "Installed. Edit /etc/tlmtry/collector.yaml then: systemctl start tlmtry-collector"
