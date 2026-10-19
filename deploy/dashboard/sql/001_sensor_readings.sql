-- Unified sensor readings for the Grafana dashboard.
--
-- The collector writes devices, metrics, and samples. Other scripts should
-- insert into external_samples and do not need a row in devices. Rows inserted
-- straight into samples without a devices row still show up as source
-- "uncatalogued".
--
-- devices / metrics / samples must stay compatible with POSTGRES_SCHEMA in
-- collector/tlmtry_collector/storage.py. CREATE TABLE IF NOT EXISTS lets this
-- script run before the collector has opened the database.
--
-- ts in external_samples is Unix seconds, the same clock as samples.ts:
--   EXTRACT(EPOCH FROM clock_timestamp())

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

CREATE TABLE IF NOT EXISTS external_samples (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    device TEXT NOT NULL DEFAULT '',
    sensor TEXT NOT NULL,
    ts DOUBLE PRECISION NOT NULL,
    value_real DOUBLE PRECISION,
    value_text TEXT,
    unit TEXT NOT NULL DEFAULT '',
    quality INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_external_samples_key_ts
    ON external_samples (source, device, sensor, ts);
CREATE INDEX IF NOT EXISTS idx_external_samples_ts_tstz
    ON external_samples ((to_timestamp(ts)));

COMMENT ON TABLE external_samples IS
    'Readings from scripts other than tlmtry-collector. ts is Unix seconds.';

DROP VIEW IF EXISTS v_sensor_readings;

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
LEFT JOIN metrics m ON m.address = s.address AND m.metric_id = s.metric_id
UNION ALL
SELECT
    e.source,
    COALESCE(NULLIF(btrim(e.device), ''), e.source) AS device,
    e.sensor,
    COALESCE(e.unit, '') AS unit,
    to_timestamp(e.ts) AS ts,
    e.value_real,
    e.value_text,
    e.quality,
    NULL::integer AS address,
    NULL::integer AS metric_id,
    false AS in_device_catalog
FROM external_samples e;

COMMENT ON VIEW v_sensor_readings IS
    'Collector samples (catalogued or not) plus external_samples. Grafana reads only this view.';

GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly;
GRANT SELECT ON v_sensor_readings TO readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO readonly;
