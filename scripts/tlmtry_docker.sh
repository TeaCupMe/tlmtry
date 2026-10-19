# Shared helpers for the PostgreSQL and Grafana containers.
# Sourced by scripts/up_postgres.sh and scripts/up_dashboard.sh.

# Grafana reaches PostgreSQL by the DNS name tlmtry-db on this network,
# whatever the container's actual name is.
tlmtry_ensure_db_network() {
    local network="$1"
    local name="$2"

    docker network inspect "$network" >/dev/null 2>&1 || docker network create "$network" >/dev/null

    local on_net="no"
    if docker inspect -f '{{range $n, $c := .NetworkSettings.Networks}}{{println $n}}{{end}}' "$name" \
        | grep -qx "$network"; then
        on_net="yes"
    fi

    # The container name is already a DNS name. An alias equal to it is rejected.
    if [[ "$name" == "tlmtry-db" ]]; then
        if [[ "$on_net" != "yes" ]]; then
            docker network connect "$network" "$name"
        fi
        return 0
    fi

    local aliases=""
    if [[ "$on_net" == "yes" ]]; then
        aliases="$(docker inspect -f '{{range $n, $c := .NetworkSettings.Networks}}{{if eq $n "'"$network"'"}}{{range $c.Aliases}}{{println .}}{{end}}{{end}}{{end}}' "$name" || true)"
    fi
    if [[ "$on_net" == "yes" ]] && grep -qx "tlmtry-db" <<<"$aliases"; then
        return 0
    fi
    if [[ "$on_net" == "yes" ]]; then
        docker network disconnect "$network" "$name"
    fi
    docker network connect --alias tlmtry-db "$network" "$name"
}

# Readonly login used by Grafana. Password is aligned with the environment
# on every run so it matches deploy/dashboard provisioning.
tlmtry_ensure_readonly_role() {
    local container="$1"
    local db_user="$2"
    local db="$3"
    local ro_user="$4"
    local ro_password="$5"

    if [[ ! "$ro_user" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "error: TLMTRY_PG_READONLY_USER must match [A-Za-z_][A-Za-z0-9_]*" >&2
        return 1
    fi
    if [[ "$ro_user" == "readonly" ]]; then
        echo "error: TLMTRY_PG_READONLY_USER cannot be 'readonly'" >&2
        return 1
    fi

    local sql
    sql="$(
        RO_USER="$ro_user" RO_PASSWORD="$ro_password" python3 - <<'PY'
import os

user = os.environ["RO_USER"]
password = os.environ["RO_PASSWORD"].replace("'", "''")
print(
    f"""SET client_min_messages TO warning;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'readonly') THEN
    CREATE ROLE readonly NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{user}') THEN
    CREATE ROLE {user} LOGIN PASSWORD '{password}';
  ELSE
    ALTER ROLE {user} WITH LOGIN PASSWORD '{password}';
  END IF;
END
$$;
GRANT readonly TO {user};
GRANT USAGE ON SCHEMA public TO readonly;
DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO readonly', current_database());
END
$$;
"""
)
PY
    )"
    printf '%s\n' "$sql" \
        | docker exec -i "$container" psql -U "$db_user" -d "$db" -v ON_ERROR_STOP=1 -q
}

tlmtry_ensure_readings_schema() {
    local container="$1"
    local db_user="$2"
    local db="$3"
    local ro_user="$4"
    local ro_password="$5"
    local sql_file="$6"

    tlmtry_ensure_readonly_role "$container" "$db_user" "$db" "$ro_user" "$ro_password"
    docker exec -i "$container" psql -U "$db_user" -d "$db" -v ON_ERROR_STOP=1 -q < "$sql_file"
}
