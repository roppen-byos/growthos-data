"""Real SQL tests ONLY in a newly owned disposable loopback database."""

import copy
import os
import unittest
from unittest.mock import patch

from growthos.contracts import FACT_FIELDS, checksum, validate_data
from growthos.errors import GrowthOSError
from fixtures import demo_payload
from growthos.postgres import PostgresStore, assert_reader_permissions
from gwosdata.postgres import WriterStore
from gwosdata.migrate_monthly import grant_roles, migrate_connection, schema_sql
from gwosdata.migrate_tiers import migrate_connection as migrate_tiers
from gwosdata.migrate_city_sentinel import migrate_connection as migrate_city_sentinel
from gwosdata.migrate_versions import migrate_connection as migrate_versions
from gwosdata.import_targets import import_connection as import_targets, parse_csv
from test_import_targets import SOURCE as TARGET_SOURCE

ENABLED = os.environ.get("GROWTHOS_TEST_ALLOW_DDL") == "disposable-local-only"


def subset(raw, months):
    raw = copy.deepcopy(raw)
    raw["months"] = months
    for key in ("facts", "month_metadata"):
        raw[key] = [r for r in raw[key] if r["month"] in months]
    return validate_data(raw)


@unittest.skipUnless(ENABLED, "Requires explicitly authorized disposable local Postgres")
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        cls.psycopg = psycopg
        cls.connections = []
        try:
            for name in ("ADMIN", "WRITER", "READER"):
                settings = conninfo_to_dict(os.environ.get(f"GROWTHOS_TEST_{name}_DSN", ""))
                if settings.get("host") not in ("localhost", "127.0.0.1", "::1") or not settings.get("dbname", "").endswith("_growthos_test"):
                    raise AssertionError("Only explicitly local disposable databases are allowed")
                cls.connections.append(psycopg.connect(**settings, autocommit=True, connect_timeout=5,
                    options="-c statement_timeout=10000 -c lock_timeout=1000"))
            cls.admin, cls.writer_connection, cls.reader_connection = cls.connections
            identities = [c.execute("SELECT current_database(), current_user").fetchone() for c in cls.connections]
            if len({r[0] for r in identities}) != 1 or len({r[1] for r in identities}) != 3:
                raise AssertionError("Use three distinct logins in one disposable database")
            if cls.admin.execute("SELECT to_regnamespace('irf')").fetchone()[0] is not None:
                raise AssertionError("Existing schema must not be overwritten")
            with cls.admin.transaction():
                cls.admin.execute(schema_sql())
                grant_roles(cls.admin, identities[1][1], identities[2][1])
            cls.roles = [r[1] for r in identities]
        except Exception:
            for conn in cls.connections:
                conn.close()
            raise AssertionError("Disposable database setup failed; details withheld") from None

    @classmethod
    def tearDownClass(cls):
        for conn in cls.connections:
            conn.close()

    def setUp(self):
        self.admin.execute("DELETE FROM irf.facts")
        self.admin.execute("DELETE FROM irf.month_metadata")
        self.writer = WriterStore(self.writer_connection, allow_nonproduction=True)
        self.reader = PostgresStore(self.reader_connection, allow_nonproduction=True)
        self.baseline = validate_data(demo_payload())
        self.writer.replace_months(self.baseline)

    def test_exact_month_replacement_removes_stale_cities_and_preserves_other_months(self):
        untouched = self.admin.execute("SELECT * FROM irf.month_metadata WHERE month='2026-08-01'").fetchone()
        raw = demo_payload()
        raw["facts"] = [r for r in raw["facts"] if not (r["month"] == "2026-09" and r["city_key"] == "vale")]
        for row in raw["facts"]:
            if row["month"] == "2026-09":
                row["installs"] = 123
        raw["month_metadata"][1]["extracted_at"] = "2026-11-03T00:01:00+00:00"
        changed = subset(raw, ["2026-09"])
        self.writer.replace_months(changed)
        result = self.reader.load(["2026-08", "2026-09", "2026-10"])
        self.assertEqual(len(result.data["facts"]), 5)
        self.assertEqual([r for r in result.data["facts"] if r["month"] == "2026-09"], changed.data["facts"])
        self.assertEqual(untouched, self.admin.execute("SELECT * FROM irf.month_metadata WHERE month='2026-08-01'").fetchone())
        self.writer.replace_months(changed)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM irf.facts").fetchone()[0], 5)

    def test_multi_month_insert_failure_rolls_back_deletes_and_metadata(self):
        raw = demo_payload()
        raw["facts"][-1]["installs"] = 999
        self.admin.execute("ALTER TABLE irf.facts ADD CONSTRAINT test_reject CHECK(installs <> 999)")
        try:
            with self.assertRaises(self.psycopg.errors.CheckViolation):
                self.writer.replace_months(subset(raw, ["2026-09", "2026-10"]))
        finally:
            self.admin.execute("ALTER TABLE irf.facts DROP CONSTRAINT test_reject")
        self.assertEqual(self.reader.load(self.baseline.data["months"]), self.baseline)

    def test_reader_permissions_are_enforced_without_session_readonly(self):
        assert_reader_permissions(self.reader_connection)
        with self.assertRaises(GrowthOSError):
            assert_reader_permissions(self.writer_connection)
        for statement in ("UPDATE irf.facts SET installs=0 WHERE false", "DELETE FROM irf.facts WHERE false",
                          "INSERT INTO irf.facts SELECT * FROM irf.facts WHERE false",
                          "DELETE FROM irf.month_metadata WHERE false", "CREATE TABLE irf.unapproved (id integer)"):
            with self.subTest(statement=statement), self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                self.reader_connection.execute(statement)

    def test_repeatable_read_does_not_mix_old_metadata_with_new_facts(self):
        raw = demo_payload()
        raw["facts"][-1]["installs"] = 345
        with self.reader_connection.transaction():
            self.reader_connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            before = self.reader.load(["2026-10"])
            self.writer.replace_months(subset(raw, ["2026-10"]))
            self.assertEqual(self.reader.load(["2026-10"]), before)
        self.assertNotEqual(self.reader.load(["2026-10"]), before)

    def test_production_writer_rejects_synthetic_before_deleting(self):
        with self.assertRaises(GrowthOSError):
            WriterStore(self.writer_connection).replace_months(self.baseline)
        self.assertEqual(self.reader.load(self.baseline.data["months"]), self.baseline)

    def test_pftu_import_grants_idempotence_conflicts_and_monthly_refresh_survival(self):
        before = self.admin.execute("SELECT * FROM irf.month_metadata ORDER BY month").fetchall()
        plans = parse_csv(TARGET_SOURCE, "platform_ftu")
        try:
            result = import_targets(self.admin, self.roles[1], self.roles[2], plans)
            self.assertTrue(result["imported"])
            self.assertEqual(len(result["merged_city_review"]), 2)
            self.assertEqual(self.reader.load_targets("2026-10"), plans[1])
            from growthos.analysis import target_facts
            facts = target_facts(self.reader.load(["2026-10"]), self.reader.load_targets("2026-10"),
                                 "2026-10", cities=["Harbor"])
            self.assertEqual(facts["row_count"], 1)
            self.assertEqual((facts["rows"][0]["target"], facts["rows"][0]["actual"]), (None, 40))
            self.assertEqual(facts["rows"][0]["target_source"], "new_launch")
            self.assertEqual(facts["overall"]["actual"], 60)
            self.assertEqual(before, self.admin.execute("SELECT * FROM irf.month_metadata ORDER BY month").fetchall())
            self.assertEqual(self.reader.load(self.baseline.data["months"]), self.baseline)
            for statement in (
                "UPDATE irf.targets SET platform_ftu_target=0 WHERE false",
                "DELETE FROM irf.targets WHERE false",
                "INSERT INTO irf.targets SELECT * FROM irf.targets WHERE false",
                "TRUNCATE irf.targets",
                "UPDATE irf.target_imports SET metric='registrations' WHERE false",
                "DELETE FROM irf.target_imports WHERE false",
                "CREATE TABLE irf.unapproved_target (id integer)",
            ):
                with self.subTest(statement=statement), self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    self.reader_connection.execute(statement)
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                self.writer_connection.execute("DELETE FROM irf.targets WHERE false")
            rerun = import_targets(self.admin, self.roles[1], self.roles[2], parse_csv(TARGET_SOURCE, "platform_ftu"))
            self.assertFalse(rerun["imported"])
            self.assertEqual(self.reader.load_targets("2026-10"), plans[1])
            with self.assertRaises(GrowthOSError) as caught:
                import_targets(self.admin, self.roles[1], self.roles[2], parse_csv(TARGET_SOURCE.replace(b"100,110", b"100,111"), "platform_ftu"))
            self.assertEqual(caught.exception.code, "MIGRATION_STATE")
            self.assertEqual(self.reader.load_targets("2026-10"), plans[1])
            self.writer.replace_months(subset(demo_payload(), ["2026-10"]))
            self.assertEqual(self.reader.load_targets("2026-10"), plans[1])
            # Stored targets carry no source fingerprint, so a deleted row is not detected after import.
            self.admin.execute("DELETE FROM irf.targets WHERE month='2026-10-01' AND city_key='ludhiana'")
            self.assertNotIn("ludhiana", {r["city_key"] for r in self.reader.load_targets("2026-10").data["rows"]})
        finally:
            # Exact new tables owned by this test in the disposable database only.
            self.admin.execute("DROP TABLE IF EXISTS irf.targets")
            self.admin.execute("DROP TABLE IF EXISTS irf.target_imports")

    def test_pftu_import_failure_rolls_back_table_and_leaves_actuals_untouched(self):
        with patch("gwosdata.import_targets._grant_reader", side_effect=GrowthOSError("READ_ONLY_ROLE", "Synthetic rejection.")):
            with self.assertRaises(GrowthOSError):
                import_targets(self.admin, self.roles[1], self.roles[2], parse_csv(TARGET_SOURCE, "platform_ftu"))
        self.assertIsNone(self.admin.execute("SELECT to_regclass('irf.targets')").fetchone()[0])
        self.assertEqual(self.reader.load(self.baseline.data["months"]), self.baseline)
        with self.assertRaises(GrowthOSError) as caught:
            self.reader.load_targets("2026-10")
        self.assertEqual(caught.exception.code, "TARGETS_UNAVAILABLE")

    def test_legacy_target_migration_preserves_values_and_marks_platform_ftu(self):
        from psycopg.types.json import Jsonb
        from gwosdata.migrate_targets import migrate_connection as migrate_targets
        self.admin.execute("""
            CREATE TABLE irf.pftu_targets (
                month date NOT NULL, kind text NOT NULL, city_key text NOT NULL, city text NOT NULL,
                target bigint, source_targets jsonb NOT NULL, source_checksum text NOT NULL,
                imported_at timestamptz NOT NULL, month_checksum text NOT NULL, month_row_count integer NOT NULL,
                PRIMARY KEY (month, kind, city_key))
        """)
        rows = [("2026-10-01", "city", "harbor", "Harbor", 80),
                ("2026-10-01", "new_launch", "", "New launch", 60),
                ("2026-10-01", "overall", "", "Grand Total", 150)]
        for month, kind, key, city, target in rows:
            self.admin.execute(
                "INSERT INTO irf.pftu_targets VALUES (%s,%s,%s,%s,%s,%s,%s,now(),%s,%s)",
                (month, kind, key, city, target, Jsonb([{"city": city, "target": target}]), "a" * 64, "b" * 64, 3))
        try:
            result = migrate_targets(self.admin, self.roles[1], self.roles[2])
            self.assertEqual(result["months"], ["2026-10"])
            self.assertIsNone(self.admin.execute("SELECT to_regclass('irf.pftu_targets')").fetchone()[0])
            loaded = self.reader.load_targets("2026-10").data
            self.assertEqual({r["kind"]: r["platform_ftu_target"] for r in loaded["rows"]},
                             {"city": 80, "new_launch": 60, "overall": 150})
            self.assertTrue(all(r["registrations_target"] is None for r in loaded["rows"]))
            self.assertEqual(self.reader.target_months(), ["2026-10"])
            self.assertEqual(self.reader.target_months("registrations"), [])
        finally:
            for table in ("irf.targets", "irf.target_imports", "irf.pftu_targets"):
                self.admin.execute(f"DROP TABLE IF EXISTS {table}")

    def test_retained_history_exceeds_per_read_month_limit(self):
        months = [f"2025-{m:02d}" for m in (10, 11, 12)] + [f"2026-{m:02d}" for m in range(1, 11)]
        for m in months:
            raw = subset(demo_payload(), ["2026-10"]).data
            raw["months"] = [m]
            raw["month_metadata"][0]["month"] = m
            for read in raw["month_metadata"][0]["source_reads"]:
                read["month"] = m
            for row in raw["facts"]:
                row["month"] = m
            self.writer.replace_months(validate_data(raw))
        self.assertEqual(self.reader.available_months(), months)
        self.assertEqual(self.reader.catalog()["available_months"], months)
        self.assertEqual(self.reader.load().data["months"], months[-2:])
        with self.assertRaises(GrowthOSError):
            self.reader.load(months)

    def legacy_tier_rows(self):
        """Install T4 fixtures only in this test-owned database, with honest old checksums."""
        self.admin.execute("ALTER TABLE irf.facts DROP CONSTRAINT facts_tier_check")
        self.admin.execute("ALTER TABLE irf.facts ADD CONSTRAINT facts_tier_check CHECK (tier IN ('T1','T2','T3','T4'))")
        self.admin.execute("UPDATE irf.facts SET tier='T4' WHERE city_key='vale' AND month > '2026-08-01'")
        raw = self.baseline.data
        for row in raw["facts"]:
            if row["city_key"] == "vale" and row["month"] > "2026-08":
                row["tier"] = "T4"
        for m in ("2026-09", "2026-10"):
            self.admin.execute("UPDATE irf.month_metadata SET result_checksum=%s WHERE month=%s",
                               (checksum([r for r in raw["facts"] if r["month"] == m]), m + "-01"))
        return raw

    def test_tier_migration_preserves_counts_lineage_times_and_unaffected_months(self):
        self.legacy_tier_rows()
        before = self.admin.execute("SELECT * FROM irf.month_metadata ORDER BY month").fetchall()
        result = migrate_tiers(self.admin, allow_nonproduction=True)
        self.assertEqual(result["changed_months"], ["2026-09", "2026-10"])
        self.assertEqual(result["relabeled_fact_count"], 2)
        migrated = self.reader.load(self.baseline.data["months"])
        self.assertEqual(migrated.data["facts"], self.baseline.data["facts"])
        after = self.admin.execute("SELECT * FROM irf.month_metadata ORDER BY month").fetchall()
        self.assertEqual(before[0], after[0])  # August had no T4 rows.
        for old, new in zip(before[1:], after[1:]):
            self.assertEqual(new[1], old[1])  # Metadata/provenance is unchanged by relabeling.
            self.assertNotEqual(old[2], new[2])  # Fact checksum changes with the tier label.
            self.assertEqual(old[3:], new[3:])  # Count and publication time remain untouched.
        self.assertEqual(migrate_tiers(self.admin, allow_nonproduction=True)["changed_months"], [])
        self.assertEqual(after, self.admin.execute("SELECT * FROM irf.month_metadata ORDER BY month").fetchall())
        with self.assertRaises(self.psycopg.errors.CheckViolation):
            self.admin.execute("UPDATE irf.facts SET tier='T4' WHERE city_key='vale'")
        assert_reader_permissions(self.reader_connection)

    def test_tier_migration_corruption_rolls_back_already_relabeled_months(self):
        self.legacy_tier_rows()
        self.admin.execute("UPDATE irf.month_metadata SET result_checksum=%s WHERE month='2026-10-01'", ('0' * 64,))
        before = self.admin.execute("SELECT * FROM irf.month_metadata ORDER BY month").fetchall()
        facts = self.admin.execute("SELECT * FROM irf.facts ORDER BY month, city_key").fetchall()
        try:
            with self.assertRaises(GrowthOSError) as caught:
                migrate_tiers(self.admin, allow_nonproduction=True)
            self.assertEqual(caught.exception.code, "STORE_INTEGRITY")
            self.assertEqual(before, self.admin.execute("SELECT * FROM irf.month_metadata ORDER BY month").fetchall())
            self.assertEqual(facts, self.admin.execute("SELECT * FROM irf.facts ORDER BY month, city_key").fetchall())
        finally:
            # Restore the new constraint so other tests start from the canonical schema.
            self.admin.execute("UPDATE irf.facts SET tier='T3' WHERE tier='T4'")
            self.admin.execute("ALTER TABLE irf.facts DROP CONSTRAINT facts_tier_check")
            self.admin.execute("ALTER TABLE irf.facts ADD CONSTRAINT facts_tier_check CHECK (tier IN ('T1','T2','T3'))")

    def test_tier_migration_rejects_reader_and_unknown_schema_state(self):
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            migrate_tiers(self.reader_connection, allow_nonproduction=True)
        self.admin.execute("CREATE TABLE irf.unexpected (id integer)")
        try:
            with self.assertRaises(GrowthOSError) as caught:
                migrate_tiers(self.admin, allow_nonproduction=True)
            self.assertEqual(caught.exception.code, "MIGRATION_STATE")
        finally:
            self.admin.execute("DROP TABLE irf.unexpected")
        self.assertEqual(self.reader.load(self.baseline.data["months"]), self.baseline)

    def test_city_sentinel_migration_merges_unrecognized_rows_and_is_idempotent(self):
        raw = demo_payload()

        def sentinel(month_value, key, city, **metrics):
            row = {field: None for field in FACT_FIELDS}
            row.update(month=month_value, city_key=key, city=city, tier="T3", source_cities=[city])
            row.update(metrics)
            return row

        # 2026-09 has only unrecognized_city (rename-only); 2026-10 has both sentinels (merge).
        raw["facts"].extend([
            sentinel("2026-09", "unrecognized_city", "UNRECOGNIZED_CITY", registrations=550),
            sentinel("2026-10", "city_not_found", "city_not_found", installs=240),
            sentinel("2026-10", "unrecognized_city", "UNRECOGNIZED_CITY", registrations=600),
        ])
        seeded = validate_data(raw)
        self.writer.replace_months(seeded)
        before = self.admin.execute("SELECT month, metadata, result_checksum, fact_count FROM irf.month_metadata ORDER BY month").fetchall()

        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            migrate_city_sentinel(self.reader_connection, allow_nonproduction=True)

        result = migrate_city_sentinel(self.admin, allow_nonproduction=True)
        self.assertEqual(result["changed_months"], ["2026-09", "2026-10"])
        self.assertEqual(result["merged_fact_count"], 2)

        sep = self.reader.load(["2026-09"])
        sep_target = next(row for row in sep.data["facts"] if row["city_key"] == "city_not_found")
        self.assertEqual((sep_target["installs"], sep_target["registrations"]), (None, 550))
        self.assertFalse(any(row["city_key"] == "unrecognized_city" for row in sep.data["facts"]))

        oct_data = self.reader.load(["2026-10"])
        oct_target = next(row for row in oct_data.data["facts"] if row["city_key"] == "city_not_found")
        self.assertEqual((oct_target["installs"], oct_target["registrations"]), (240, 600))
        self.assertFalse(any(row["city_key"] == "unrecognized_city" for row in oct_data.data["facts"]))

        after = self.admin.execute("SELECT month, metadata, result_checksum, fact_count FROM irf.month_metadata ORDER BY month").fetchall()
        self.assertEqual(before[0], after[0])  # Untouched August is completely identical.
        for old, new in zip(before, after):
            self.assertEqual(new[1], old[1])  # Provenance metadata is unchanged.
        self.assertNotEqual(after[1][2], before[1][2])  # September checksum updated.
        self.assertEqual(after[1][3], before[1][3])  # Rename-only keeps fact_count equal.
        self.assertNotEqual(after[2][2], before[2][2])  # October checksum updated.
        self.assertEqual(after[2][3], before[2][3] - 1)  # Merge drops one fact in October.
        self.assertEqual(migrate_city_sentinel(self.admin, allow_nonproduction=True)["changed_months"], [])
        self.assertEqual(after, self.admin.execute("SELECT month, metadata, result_checksum, fact_count FROM irf.month_metadata ORDER BY month").fetchall())
        assert_reader_permissions(self.reader_connection)

    def test_city_sentinel_migration_corruption_rolls_back_earlier_month(self):
        raw = demo_payload()
        for m, key, city in (("2026-09", "unrecognized_city", "UNRECOGNIZED_CITY"),
                             ("2026-10", "unrecognized_city", "UNRECOGNIZED_CITY")):
            row = {field: None for field in FACT_FIELDS}
            row.update(month=m, city_key=key, city=city, tier="T3", source_cities=[city], registrations=100)
            raw["facts"].append(row)
        self.writer.replace_months(validate_data(raw))
        self.admin.execute("UPDATE irf.month_metadata SET result_checksum=%s WHERE month='2026-10-01'", ("0" * 64,))
        before_meta = self.admin.execute("SELECT * FROM irf.month_metadata ORDER BY month").fetchall()
        before_facts = self.admin.execute("SELECT * FROM irf.facts ORDER BY month, city_key").fetchall()
        with self.assertRaises(GrowthOSError) as caught:
            migrate_city_sentinel(self.admin, allow_nonproduction=True)
        self.assertEqual(caught.exception.code, "STORE_INTEGRITY")
        self.assertEqual(before_meta, self.admin.execute("SELECT * FROM irf.month_metadata ORDER BY month").fetchall())
        self.assertEqual(before_facts, self.admin.execute("SELECT * FROM irf.facts ORDER BY month, city_key").fetchall())

    def test_version_migration_strips_legacy_keys_and_preserves_everything_else(self):
        from psycopg.types.json import Jsonb
        for month in ("2026-09-01", "2026-10-01"):
            meta = self.admin.execute("SELECT metadata FROM irf.month_metadata WHERE month=%s", (month,)).fetchone()[0]
            legacy_meta = {
                **meta,
                "definition_version": "irf-definitions-dec005-dec006-v1",
                "mapping_version": "irf-city-classification-v2",
                "reference_version": "live-cities-x",
                "source_reads": [{**r, "query_version": "irf-extraction-v1"} for r in meta["source_reads"]],
            }
            self.admin.execute("UPDATE irf.month_metadata SET metadata=%s WHERE month=%s", (Jsonb(legacy_meta), month))
        with self.assertRaises(GrowthOSError) as caught:
            PostgresStore(self.admin, allow_nonproduction=True).load(["2026-08", "2026-09", "2026-10"])
        self.assertEqual(caught.exception.code, "SCHEMA")
        before = self.admin.execute("SELECT month, result_checksum, fact_count, published_at FROM irf.month_metadata ORDER BY month").fetchall()
        result = migrate_versions(self.admin, allow_nonproduction=True)
        self.assertEqual(result["changed_months"], ["2026-09", "2026-10"])
        self.assertEqual(result["removed_fields"], ["definition_version", "mapping_version", "reference_version", "source_reads.query_version"])
        self.assertEqual(before, self.admin.execute("SELECT month, result_checksum, fact_count, published_at FROM irf.month_metadata ORDER BY month").fetchall())
        for meta in (r[0] for r in self.admin.execute("SELECT metadata FROM irf.month_metadata ORDER BY month").fetchall()):
            for removed in ("definition_version", "mapping_version", "reference_version"):
                self.assertNotIn(removed, meta)
            for read in meta["source_reads"]:
                self.assertNotIn("query_version", read)
        self.assertEqual(PostgresStore(self.admin, allow_nonproduction=True).load(self.baseline.data["months"]), self.baseline)
        self.assertEqual(migrate_versions(self.admin, allow_nonproduction=True)["changed_months"], [])
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            migrate_versions(self.reader_connection, allow_nonproduction=True)

    def test_migration_preserves_active_data_and_rejects_corruption(self):
        from psycopg.types.json import Jsonb
        raw = demo_payload()
        raw["nonproduction"] = False
        expected = validate_data(raw)
        legacy = {"nonproduction": False, "requested_months": raw["months"], "created_at": raw["month_metadata"][0]["extracted_at"],
                  "business_timezone": raw["month_metadata"][0]["business_timezone"],
                  "definition_version": "live-definitions-v1", "mapping_version": "live-mapping-v1", "reference_version": "live-reference-v1",
                  "source_reads": [{**read, "query_version": "live-" + read["source"] + "-v1", "cutoff": read["completed_at"]}
                                   for meta in raw["month_metadata"] for read in meta["source_reads"]]}
        class RestoreTestDatabase(Exception):
            pass
        with self.assertRaises(RestoreTestDatabase), self.admin.transaction():
            # All objects below belong exclusively to this disposable test database.
            self.admin.execute("DROP TABLE irf.facts; DROP TABLE irf.month_metadata")
            self.admin.execute("""
                CREATE TABLE irf.refresh_runs (run_id text PRIMARY KEY, snapshot_id text);
                CREATE TABLE irf.snapshots (snapshot_id text PRIMARY KEY REFERENCES irf.refresh_runs(run_id), metadata jsonb, result_checksum text, fact_count integer);
                ALTER TABLE irf.refresh_runs ADD CONSTRAINT published_snapshot_fk FOREIGN KEY (snapshot_id) REFERENCES irf.snapshots(snapshot_id);
                CREATE TABLE irf.active_snapshot (singleton boolean, snapshot_id text REFERENCES irf.snapshots(snapshot_id));
                CREATE TABLE irf.facts AS SELECT 'legacy'::text AS snapshot_id, * FROM (SELECT NULL::date AS month, ''::text AS city_key, ''::text AS city,
                    ''::text AS tier, '[]'::jsonb AS source_cities, 0::bigint AS installs, 0::bigint AS registrations,
                    0::bigint AS platform_ftu, 0::bigint AS platform_link_ftu, 0::bigint AS platform_auto_ftu, 0::bigint AS platform_cab_ftu,
                    0::bigint AS fe_sessions, 0::bigint AS rr, 0::bigint AS net, 0::bigint AS bike_rr, 0::bigint AS bike_net,
                    0::bigint AS auto_rr, 0::bigint AS auto_net, 0::bigint AS cab_rr, 0::bigint AS cab_net) empty WHERE false;
                CREATE FUNCTION irf.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
                CREATE FUNCTION irf.check_candidate_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
                CREATE FUNCTION irf.check_run_transition() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;
            """)
            self.admin.execute("INSERT INTO irf.refresh_runs VALUES ('legacy', NULL)")
            self.admin.execute("INSERT INTO irf.snapshots VALUES ('legacy',%s,%s,%s)", (Jsonb(legacy), '0' * 64, len(raw["facts"])))
            self.admin.execute("INSERT INTO irf.active_snapshot VALUES (true,'legacy')")
            fields = sorted(raw["facts"][0])
            for row in raw["facts"]:
                values = {**row, "month": row["month"] + '-01', "source_cities": Jsonb(row["source_cities"])}
                self.admin.execute(f"INSERT INTO irf.facts (snapshot_id,{','.join(fields)}) VALUES ('legacy',{','.join(['%s'] * len(fields))})", tuple(values[k] for k in fields))
            with self.assertRaises(GrowthOSError):
                migrate_connection(self.admin, self.roles[1], self.roles[2])
            self.assertIsNotNone(self.admin.execute("SELECT to_regclass('irf.snapshots')").fetchone()[0])
            self.admin.execute("UPDATE irf.snapshots SET result_checksum=%s", (expected.result_checksum,))
            result = migrate_connection(self.admin, self.roles[1], self.roles[2])
            self.assertEqual(result["result_checksum"], expected.result_checksum)
            self.assertEqual(PostgresStore(self.admin).load(raw["months"]), expected)
            self.assertIsNone(self.admin.execute("SELECT to_regclass('irf.snapshots')").fetchone()[0])
            raise RestoreTestDatabase()


if __name__ == "__main__":
    unittest.main()
