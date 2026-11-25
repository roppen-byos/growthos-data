-- Additive funnel metric columns. Owner-only migration; never run as the reader.
ALTER TABLE irf.facts ADD COLUMN IF NOT EXISTS fe_sessions bigint CHECK (fe_sessions >= 0);
ALTER TABLE irf.facts ADD COLUMN IF NOT EXISTS rr bigint CHECK (rr >= 0);
ALTER TABLE irf.facts ADD COLUMN IF NOT EXISTS net bigint CHECK (net >= 0);
ALTER TABLE irf.facts ADD COLUMN IF NOT EXISTS bike_rr bigint CHECK (bike_rr >= 0);
ALTER TABLE irf.facts ADD COLUMN IF NOT EXISTS bike_net bigint CHECK (bike_net >= 0);
ALTER TABLE irf.facts ADD COLUMN IF NOT EXISTS auto_rr bigint CHECK (auto_rr >= 0);
ALTER TABLE irf.facts ADD COLUMN IF NOT EXISTS auto_net bigint CHECK (auto_net >= 0);
ALTER TABLE irf.facts ADD COLUMN IF NOT EXISTS cab_rr bigint CHECK (cab_rr >= 0);
ALTER TABLE irf.facts ADD COLUMN IF NOT EXISTS cab_net bigint CHECK (cab_net >= 0);
