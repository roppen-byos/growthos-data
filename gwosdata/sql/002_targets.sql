-- Apply after 001_reporting.sql, which creates schema irf.
-- Independent target data. No FK to replaceable actual-month metadata.
-- The owner import owns the transaction and explicitly grants reader SELECT.
-- One row carries both metrics so the target ratio is read atomically.
CREATE TABLE irf.targets (
    month date NOT NULL CHECK (EXTRACT(DAY FROM month) = 1),
    kind text NOT NULL CHECK (kind IN ('city', 'new_launch', 'overall')),
    city_key text NOT NULL,
    city text NOT NULL CHECK (length(city) BETWEEN 1 AND 120),
    platform_ftu_target bigint CHECK (platform_ftu_target >= 0),
    registrations_target bigint CHECK (registrations_target >= 0),
    PRIMARY KEY (month, kind, city_key),
    CHECK ((kind = 'city' AND city_key ~ '^[a-z0-9][a-z0-9_]{0,119}$')
        OR (kind = 'new_launch' AND city_key = '' AND city = 'New launch')
        OR (kind = 'overall' AND city_key = '' AND city = 'Grand Total'))
);

-- Presence markers only: distinguish an unimported metric-month from a blank target.
CREATE TABLE irf.target_imports (
    month date NOT NULL CHECK (EXTRACT(DAY FROM month) = 1),
    metric text NOT NULL CHECK (metric IN ('platform_ftu', 'registrations')),
    PRIMARY KEY (month, metric)
);

REVOKE ALL ON irf.targets FROM PUBLIC;
REVOKE ALL ON irf.target_imports FROM PUBLIC;
