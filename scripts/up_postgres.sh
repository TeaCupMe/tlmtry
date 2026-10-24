#!/usr/bin/env bash
# Start a PostgreSQL container and fill it with pseudo telemetry.
#
# Usage:
#   bash scripts/up_postgres.sh
#   bash scripts/up_postgres.sh --hours 6
#
# Extra arguments are passed to scripts/seed_database.py. The database URL and
# --reset are always set by this script, so a second run replaces the rows.
#
# Environment:
#   TLMTRY_PG_IMAGE      image (default postgres:16)
#   TLMTRY_PG_CONTAINER  container name (default tlmtry-postgres)
#   TLMTRY_PG_PORT       host port (default 5432)
#   TLMTRY_PG_USER       role (default tlmtry)
#   TLMTRY_PG_PASSWORD   password (default tlmtry)
#   TLMTRY_PG_DB         database name (default tlmtry)
#   TLMTRY_PG_VOLUME     named volume (default tlmtry-pg-data)
#   TLMTRY_DOCKER_NETWORK
#                        network shared with Grafana (default tlmtry)
#   TLMTRY_PG_READONLY_USER
#                        dashboard login (default us)
#   TLMTRY_PG_READONLY_PASSWORD
#                        dashboard password (default matches scripts/init_readonly_user.sql)
#   PYTHON               python interpreter (default python3)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=tlmtry_docker.sh
source "$ROOT/scripts/tlmtry_docker.sh"

IMAGE="${TLMTRY_PG_IMAGE:-postgres:16}"
NAME="${TLMTRY_PG_CONTAINER:-tlmtry-postgres}"
PORT="${TLMTRY_PG_PORT:-5432}"
USER_NAME="${TLMTRY_PG_USER:-tlmtry}"
PASSWORD="${TLMTRY_PG_PASSWORD:-tlmtry}"
DB="${TLMTRY_PG_DB:-tlmtry}"
VOLUME="${TLMTRY_PG_VOLUME:-tlmtry-pg-data}"
NETWORK="${TLMTRY_DOCKER_NETWORK:-tlmtry}"
RO_USER="${TLMTRY_PG_READONLY_USER:-us}"
RO_PASSWORD="${TLMTRY_PG_READONLY_PASSWORD:-H2wf6w90dw4}"
PYTHON="${PYTHON:-python3}"
SQL_FILE="$ROOT/deploy/dashboard/sql/001_sensor_readings.sql"

usage() {
    cat <<EOF
Usage: bash scripts/up_postgres.sh [seed options]

Start the ${NAME} PostgreSQL container and load pseudo telemetry.

The container publishes port ${PORT}, with user, password, and database
"${USER_NAME}". Data is stored in the Docker volume ${VOLUME}. Re-running
the script starts the same container and replaces the telemetry rows.

The container joins the Docker network ${NETWORK} as tlmtry-db and installs
the sensor view used by scripts/up_dashboard.sh.

Extra arguments are forwarded to scripts/seed_database.py, for example:

    bash scripts/up_postgres.sh --hours 6

Stop the container:  docker stop ${NAME}
Remove it:           docker rm -f ${NAME} && docker volume rm ${VOLUME}
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is not installed or not on PATH" >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "error: cannot talk to the docker daemon. Is it running, and can this user access it?" >&2
    exit 1
fi

#if ! "$PYTHON" -c "import psycopg" >/dev/null 2>&1; then
#    echo "Installing psycopg so the seed script can connect to PostgreSQL..."
#    "$PYTHON" -m pip install 'psycopg[binary]>=3.1'
#fi

docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
network_alias=()
if [[ "$NAME" != "tlmtry-db" ]]; then
    network_alias=(--network-alias tlmtry-db)
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "Starting existing container ${NAME}"
    docker start "$NAME" >/dev/null
else
    echo "Creating container ${NAME} from ${IMAGE} on port ${PORT}"

    # echo "using init script: ${ROOT}/scripts/init-readonly.sql"
    # cat "${ROOT}/scripts_db/init_readonly_user.sql"

    docker run -d \
        --name "$NAME" \
        --restart unless-stopped \
        --network "$NETWORK" \
        "${network_alias[@]}" \
        -e "POSTGRES_USER=${USER_NAME}" \
        -e "POSTGRES_PASSWORD=${PASSWORD}" \
        -e "POSTGRES_DB=${DB}" \
        -p "${PORT}:5432" \
        -v "${VOLUME}:/var/lib/postgresql/data" \
	-v "${ROOT}/scripts/init_readonly_user.sql:/docker-entrypoint-initdb.d/init-readonly.sql:ro"\
        --health-cmd "pg_isready -U ${USER_NAME} -d ${DB}" \
        --health-interval 2s \
        --health-timeout 5s \
        --health-retries 30 \
        "$IMAGE" >/dev/null
fi

mapped="$(docker port "$NAME" 5432/tcp 2>/dev/null | head -n 1 || true)"
if [[ -n "$mapped" ]]; then
    PORT="${mapped##*:}"
fi

echo "Waiting for PostgreSQL..."
ready=0
for _ in $(seq 1 60); do
    if docker exec "$NAME" pg_isready -U "$USER_NAME" -d "$DB" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
if [[ "$ready" -ne 1 ]]; then
    echo "error: PostgreSQL in ${NAME} did not become ready within 60s" >&2
    docker logs --tail 40 "$NAME" >&2 || true
    exit 1
fi

database_url="$(
    TLMTRY_PG_USER="$USER_NAME" \
    TLMTRY_PG_PASSWORD="$PASSWORD" \
    TLMTRY_PG_PORT="$PORT" \
    TLMTRY_PG_DB="$DB" \
    "$PYTHON" - <<'PY'
import os
import urllib.parse

user = urllib.parse.quote(os.environ["TLMTRY_PG_USER"], safe="")
password = urllib.parse.quote(os.environ["TLMTRY_PG_PASSWORD"], safe="")
db = urllib.parse.quote(os.environ["TLMTRY_PG_DB"], safe="")
port = os.environ["TLMTRY_PG_PORT"]
print(f"postgresql://{user}:{password}@127.0.0.1:{port}/{db}")
PY
)"

# echo "Loading pseudo telemetry into ${database_url}"
# "$PYTHON" "$ROOT/scripts/seed_database.py" --reset "$@" --database "$database_url"

tlmtry_ensure_db_network "$NETWORK" "$NAME"
echo "Installing sensor readings view"
tlmtry_ensure_readings_schema "$NAME" "$USER_NAME" "$DB" "$RO_USER" "$RO_PASSWORD" "$SQL_FILE"

echo
echo "PostgreSQL is ready: ${database_url}"
echo "Dashboard: bash scripts/up_dashboard.sh"
echo "Stop the container with: docker stop ${NAME}"
