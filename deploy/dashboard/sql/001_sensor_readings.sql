-- Unified sensor readings for the Grafana dashboard.
--
-- Every writer, the collector and any other script, inserts into samples.
-- Rows whose address is in devices are labelled "collector" and pick up the
-- metric name and unit. Rows with no devices entry are labelled "uncatalogued"
-- and shown as "address N / metric M".
--
-- devices / metrics / samples must stay compatible with POSTGRES_SCHEMA in
-- collector/tlmtry_collector/storage.py. CREATE TABLE IF NOT EXISTS lets this
-- script run before the collector has opened the database.
--
-- samples.ts is Unix seconds: EXTRACT(EPOCH FROM clock_timestamp())

SET client_min_messages TO warning;

CREATE TABLE IF NOT EXISTS devices (
    address INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    fw_major INTEGER NOT NULL,
    fw_minor INTEGER NOT NULL,
    poll_interval_ms INTEGER NOT NULL,
    last_seen DOUBLE PRECISION,
    online INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    address INTEGER NOT NULL,
    metric_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type INTEGER NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY (address, metric_id),
    FOREIGN KEY (address) REFERENCES devices(address)
);

CREATE TABLE IF NOT EXISTS samples (
    id BIGSERIAL PRIMARY KEY,
    address INTEGER NOT NULL,
    metric_id INTEGER NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    value_real DOUBLE PRECISION,
    value_text TEXT,
    type INTEGER NOT NULL,
    quality INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_samples_addr_ts ON samples(address, ts);
CREATE INDEX IF NOT EXISTS idx_samples_ts_tstz ON samples ((to_timestamp(ts)));

DROP VIEW IF EXISTS v_sensor_readings;
DROP TABLE IF EXISTS external_samples;

CREATE VIEW v_sensor_readings AS
SELECT
    CASE
        WHEN d.address IS NOT NULL THEN 'collector'::text
        ELSE 'uncatalogued'::text
    END AS source,
    COALESCE(d.name, 'address ' || s.address::text) AS device,
    COALESCE(m.name, 'metric ' || s.metric_id::text) AS sensor,
    COALESCE(m.unit, '') AS unit,
    to_timestamp(s.ts) AS ts,
    s.value_real,
    s.value_text,
    s.quality,
    s.address,
    s.metric_id,
    (d.address IS NOT NULL) AS in_device_catalog
FROM samples s
LEFT JOIN devices d ON d.address = s.address
LEFT JOIN metrics m ON m.address = s.address AND m.metric_id = s.metric_id;

COMMENT ON VIEW v_sensor_readings IS
    'Every samples row. Names come from devices/metrics when that address is catalogued.';

GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly;
GRANT SELECT ON v_sensor_readings TO readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO readonly;
