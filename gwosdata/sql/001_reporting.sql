-- Apply once as owner to an approved empty database. Never a migration script.
BEGIN;
CREATE SCHEMA irf;
REVOKE ALL ON SCHEMA irf FROM PUBLIC;

CREATE TABLE irf.month_metadata (
    month date PRIMARY KEY CHECK (EXTRACT(DAY FROM month) = 1),
    metadata jsonb NOT NULL CHECK (jsonb_typeof(metadata) = 'object' AND octet_length(metadata::text) <= 131072),
    result_checksum text NOT NULL CHECK (result_checksum ~ '^[a-f0-9]{64}$'),
    fact_count integer NOT NULL CHECK (fact_count BETWEEN 1 AND 20000),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE irf.facts (
    month date NOT NULL REFERENCES irf.month_metadata(month) CHECK (EXTRACT(DAY FROM month) = 1),
    city_key text NOT NULL CHECK (length(city_key) BETWEEN 1 AND 120),
    city text NOT NULL CHECK (length(city) BETWEEN 1 AND 120),
    tier text NOT NULL CHECK (tier IN ('T1','T2','T3')),
    source_cities jsonb NOT NULL CHECK (
        jsonb_typeof(source_cities) = 'array'
        AND jsonb_array_length(source_cities) BETWEEN 1 AND 100
        AND octet_length(source_cities::text) <= 65536
    ),
    installs bigint CHECK (installs >= 0),
    registrations bigint CHECK (registrations >= 0),
    platform_ftu bigint CHECK (platform_ftu >= 0),
    platform_link_ftu bigint CHECK (platform_link_ftu >= 0),
    platform_auto_ftu bigint CHECK (platform_auto_ftu >= 0),
    platform_cab_ftu bigint CHECK (platform_cab_ftu >= 0),
    fe_sessions bigint CHECK (fe_sessions >= 0),
    rr bigint CHECK (rr >= 0),
    net bigint CHECK (net >= 0),
    bike_rr bigint CHECK (bike_rr >= 0),
    bike_net bigint CHECK (bike_net >= 0),
    auto_rr bigint CHECK (auto_rr >= 0),
    auto_net bigint CHECK (auto_net >= 0),
    cab_rr bigint CHECK (cab_rr >= 0),
    cab_net bigint CHECK (cab_net >= 0),
    PRIMARY KEY (month, city_key)
);

REVOKE ALL ON ALL TABLES IN SCHEMA irf FROM PUBLIC;
COMMIT;
