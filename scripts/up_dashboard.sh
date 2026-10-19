#!/usr/bin/env bash
# Start the Grafana dashboard container against the tlmtry PostgreSQL.
#
# Usage:
#   bash scripts/up_dashboard.sh
#
# The dashboard reads v_sensor_readings: every row in samples, joined to
# devices and metrics when that address was catalogued.
# If the PostgreSQL container is missing, this script starts it first.
#
# Environment (PostgreSQL side matches scripts/up_postgres.sh):
#   TLMTRY_PG_CONTAINER          container name (default tlmtry-postgres)
#   TLMTRY_PG_USER               owner role (default tlmtry)
#   TLMTRY_PG_DB                 database name (default tlmtry)
#   TLMTRY_PG_READONLY_USER      Grafana login (default us)
#   TLMTRY_PG_READONLY_PASSWORD  Grafana password (default matches init SQL)
#   TLMTRY_DOCKER_NETWORK        docker network (default tlmtry)
#   TLMTRY_GRAFANA_IMAGE         image (default grafana/grafana:11.4.0)
#   TLMTRY_GRAFANA_CONTAINER     container name (default tlmtry-grafana)
#   TLMTRY_GRAFANA_PORT          host port (default 3000)
#   TLMTRY_GRAFANA_ADMIN_USER    Grafana admin (default admin)
#   TLMTRY_GRAFANA_ADMIN_PASSWORD
#                                Grafana admin password (default admin)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=tlmtry_docker.sh
source "$ROOT/scripts/tlmtry_docker.sh"

PG_NAME="${TLMTRY_PG_CONTAINER:-tlmtry-postgres}"
PG_USER="${TLMTRY_PG_USER:-tlmtry}"
DB="${TLMTRY_PG_DB:-tlmtry}"
RO_USER="${TLMTRY_PG_READONLY_USER:-us}"
RO_PASSWORD="${TLMTRY_PG_READONLY_PASSWORD:-H2wf6w90dw4}"
NETWORK="${TLMTRY_DOCKER_NETWORK:-tlmtry}"

GIMAGE="${TLMTRY_GRAFANA_IMAGE:-grafana/grafana:11.4.0}"
GNAME="${TLMTRY_GRAFANA_CONTAINER:-tlmtry-grafana}"
GPORT="${TLMTRY_GRAFANA_PORT:-3000}"
ADMIN_USER="${TLMTRY_GRAFANA_ADMIN_USER:-admin}"
ADMIN_PASSWORD="${TLMTRY_GRAFANA_ADMIN_PASSWORD:-admin}"

SQL_FILE="$ROOT/deploy/dashboard/sql/001_sensor_readings.sql"

usage() {
    cat <<EOF
Usage: bash scripts/up_dashboard.sh

Start the ${GNAME} Grafana container and open the sensor dashboard.

Grafana joins the Docker network ${NETWORK} and reads PostgreSQL through the
DNS name tlmtry-db (container ${PG_NAME}). The page is http://127.0.0.1:${GPORT}
with anonymous view access. Sign in as ${ADMIN_USER} to edit.

The readings view is every row in samples:
  - joined to devices/metrics when that address is catalogued
  - otherwise shown as address N / metric M

Stop the container:  docker stop ${GNAME}
Remove it:           docker rm -f ${GNAME}
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ $# -gt 0 ]]; then
    echo "error: unexpected argument: $1" >&2
    usage >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is not installed or not on PATH" >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "error: cannot talk to the docker daemon. Is it running, and can this user access it?" >&2
    exit 1
fi

if [[ ! -f "$SQL_FILE" ]]; then
    echo "error: missing ${SQL_FILE}" >&2
    exit 1
fi

if ! docker ps -a --format '{{.Names}}' | grep -qx "$PG_NAME"; then
    echo "PostgreSQL container ${PG_NAME} is not here; starting it"
    bash "$ROOT/scripts/up_postgres.sh"
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$PG_NAME"; then
    echo "Starting PostgreSQL container ${PG_NAME}"
    docker start "$PG_NAME" >/dev/null
fi

echo "Waiting for PostgreSQL..."
ready=0
for _ in $(seq 1 60); do
    if docker exec "$PG_NAME" pg_isready -U "$PG_USER" -d "$DB" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
if [[ "$ready" -ne 1 ]]; then
    echo "error: PostgreSQL in ${PG_NAME} did not become ready within 60s" >&2
    docker logs --tail 40 "$PG_NAME" >&2 || true
    exit 1
fi

tlmtry_ensure_db_network "$NETWORK" "$PG_NAME"
echo "Installing sensor readings view"
tlmtry_ensure_readings_schema "$PG_NAME" "$PG_USER" "$DB" "$RO_USER" "$RO_PASSWORD" "$SQL_FILE"

if docker ps -a --format '{{.Names}}' | grep -qx "$GNAME"; then
    echo "Replacing container ${GNAME} so provisioning and env match this repo"
    docker rm -f "$GNAME" >/dev/null
fi

echo "Creating container ${GNAME} from ${GIMAGE} on port ${GPORT}"
docker run -d \
    --name "$GNAME" \
    --restart unless-stopped \
    --network "$NETWORK" \
    -p "${GPORT}:3000" \
    -e GF_AUTH_ANONYMOUS_ENABLED=true \
    -e GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer \
    -e GF_AUTH_DISABLE_LOGIN_FORM=false \
    -e GF_USERS_ALLOW_SIGN_UP=false \
    -e GF_USERS_DEFAULT_THEME=dark \
    -e GF_SECURITY_ADMIN_USER="$ADMIN_USER" \
    -e GF_SECURITY_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
    -e GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/etc/grafana/dashboards/sensors.json \
    -e GF_ANALYTICS_REPORTING_ENABLED=false \
    -e GF_ANALYTICS_CHECK_FOR_UPDATES=false \
    -e GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false \
    -e GF_PLUGINS_PREINSTALL_DISABLED=true \
    -e "TLMTRY_PG_READONLY_USER=${RO_USER}" \
    -e "TLMTRY_PG_READONLY_PASSWORD=${RO_PASSWORD}" \
    -e "TLMTRY_PG_DB=${DB}" \
    -v "$ROOT/deploy/dashboard/provisioning/datasources/postgres.yml:/etc/grafana/provisioning/datasources/postgres.yml:ro" \
    -v "$ROOT/deploy/dashboard/provisioning/dashboards/provider.yml:/etc/grafana/provisioning/dashboards/provider.yml:ro" \
    -v "$ROOT/deploy/dashboard/dashboards:/etc/grafana/dashboards:ro" \
    "$GIMAGE" >/dev/null

echo "Waiting for Grafana..."
export TLMTRY_GRAFANA_PORT="$GPORT"
export TLMTRY_GRAFANA_ADMIN_USER="$ADMIN_USER"
export TLMTRY_GRAFANA_ADMIN_PASSWORD="$ADMIN_PASSWORD"
if ! python3 - <<'PY'
import base64
import json
import os
import time
import urllib.error
import urllib.request

port = os.environ["TLMTRY_GRAFANA_PORT"]
user = os.environ["TLMTRY_GRAFANA_ADMIN_USER"]
password = os.environ["TLMTRY_GRAFANA_ADMIN_PASSWORD"]
base = f"http://127.0.0.1:{port}"

token = base64.b64encode(f"{user}:{password}".encode()).decode()

def request(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Basic {token}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc

deadline = time.time() + 120
last_error = "not started"
while time.time() < deadline:
    try:
        status, body = request("GET", "/api/health")
        if status == 200 and '"database"' in body:
            break
        last_error = body
    except Exception as exc:  # noqa: BLE001 - startup probe
        last_error = str(exc)
    time.sleep(1)
else:
    raise SystemExit(f"Grafana health check failed: {last_error}")

deadline = time.time() + 60
while time.time() < deadline:
    try:
        status, body = request("GET", "/api/dashboards/uid/tlmtry-sensors")
        if status == 200:
            dashboard = json.loads(body)
            break
        last_error = body
    except Exception as exc:  # noqa: BLE001 - provisioning may lag health
        last_error = str(exc)
    time.sleep(1)
else:
    raise SystemExit(f"dashboard tlmtry-sensors was not provisioned: {last_error}")

home_id = dashboard.get("dashboard", {}).get("id")
prefs = {"homeDashboardUID": "tlmtry-sensors", "timezone": "browser"}
if home_id:
    prefs["homeDashboardId"] = home_id
request("PUT", "/api/org/preferences", prefs)
print("Grafana is serving the sensor dashboard")
PY
then
    echo "error: Grafana in ${GNAME} did not become ready" >&2
    docker logs --tail 60 "$GNAME" >&2 || true
    exit 1
fi

echo
echo "Dashboard: http://127.0.0.1:${GPORT}/d/tlmtry-sensors/datchiki"
echo "Anonymous users can view. Admin login: ${ADMIN_USER} (TLMTRY_GRAFANA_ADMIN_PASSWORD)."
echo "Stop the container with: docker stop ${GNAME}"
