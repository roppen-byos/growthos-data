"""Real SQL tests ONLY in a newly owned disposable loopback database."""

import copy
import os
import unittest
from unittest.mock import patch

from gwosdata.contracts import reporting_schema_sql, validate_data
from gwosdata.errors import GrowthOSError
from fixtures import demo_payload
from postgres_reader import PostgresStore, assert_reader_permissions
from gwosdata.postgres import WriterStore
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
            cls.admin.execute(reporting_schema_sql())
            from psycopg import sql
            writer, reader = (sql.Identifier(row[1]) for row in identities[1:])
            with cls.admin.transaction():
                cls.admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                cls.admin.execute(sql.SQL("GRANT USAGE ON SCHEMA irf TO {}, {}").format(writer, reader))
                cls.admin.execute(sql.SQL("GRANT SELECT ON irf.facts, irf.month_metadata TO {}").format(reader))
                cls.admin.execute(sql.SQL("GRANT SELECT, INSERT, DELETE ON irf.facts, irf.month_metadata TO {}").format(writer))
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
            self.assertNotIn("merged_city_review", result)
            self.assertEqual(self.reader.load_targets("2026-10"), plans[1])
            # Verify storage without importing the consumer's selection engine.
            actuals = self.reader.load(["2026-10"]).data["facts"]
            self.assertEqual(next(r["platform_ftu"] for r in actuals if r["city_key"] == "harbor"), 40)
            self.assertEqual(sum(r["platform_ftu"] for r in actuals), 60)
            rows = self.reader.load_targets("2026-10").data["rows"]
            self.assertFalse(any(r["city_key"] == "harbor" for r in rows))
            self.assertEqual(next(r["platform_ftu_target"] for r in rows if r["kind"] == "new_launch"), 50)
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
        self.assertEqual(self.reader.load().data["months"], months[-2:])
        with self.assertRaises(GrowthOSError):
            self.reader.load(months)


if __name__ == "__main__":
    unittest.main()
