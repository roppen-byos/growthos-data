from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch

from fixtures import demo_payload
from gwosdata.contracts import FACT_FIELDS, checksum, month, validate_data
from gwosdata.errors import GrowthOSError
from gwosdata.migrate_monthly import run
from gwosdata.postgres import PostgresStore, validate_reader_settings
from gwosdata.targets import TARGET_DB_FIELDS
from gwosdata.import_targets import parse_csv
from test_import_targets import SOURCE


class VerificationReadTests(unittest.TestCase):
    def prepare_load(self):
        dataset = validate_data(demo_payload())
        data = dataset.data
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        stored = [(month(meta["month"]), meta,
                   checksum([r for r in data["facts"] if r["month"] == meta["month"]]),
                   sum(r["month"] == meta["month"] for r in data["facts"]))
                  for meta in data["month_metadata"]]
        cursor.fetchall.return_value = stored
        fields = sorted(FACT_FIELDS)
        cursor.fetchmany.return_value = [tuple(month(r[k]) if k == "month" else r[k] for k in fields)
                                         for r in data["facts"]]
        return connection, cursor, stored, dataset

    def test_verification_load_is_bounded_and_matches_published_contract(self):
        connection, cursor, _, expected = self.prepare_load()
        self.assertEqual(PostgresStore(connection, allow_nonproduction=True).load(expected.data["months"]), expected)
        self.assertIn("WHERE month = ANY(%s)", cursor.execute.call_args.args[0])
        cursor.fetchmany.assert_called_once_with(20_001)

    def test_corrupt_stored_counts_checksums_and_missing_months_are_rejected(self):
        for fault in ("count", "checksum", "month"):
            connection, _, stored, dataset = self.prepare_load()
            date, meta, digest, count = stored[0]
            if fault == "month":
                stored.pop()
            else:
                stored[0] = (date, meta, "0" * 64 if fault == "checksum" else digest,
                             count + 1 if fault == "count" else count)
            with self.subTest(fault=fault), self.assertRaises(GrowthOSError) as caught:
                PostgresStore(connection, allow_nonproduction=True).load(dataset.data["months"])
            self.assertEqual(caught.exception.code, "COVERAGE" if fault == "month" else "STORE_INTEGRITY")

    def test_target_read_checks_reader_permissions_and_bounds(self):
        expected = parse_csv(SOURCE, "platform_ftu")[1]
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("irf.targets", "irf.target_imports"), (True,)]
        cursor.fetchmany.return_value = [tuple(month(r[k]) if k == "month" else r[k] for k in TARGET_DB_FIELDS)
                                         for r in expected.data["rows"]]
        self.assertEqual(PostgresStore(connection).load_targets("2026-10"), expected)
        cursor.fetchmany.assert_called_once_with(20_001)
        cursor.fetchone.side_effect = [("irf.targets", "irf.target_imports"), (False,)]
        with self.assertRaises(GrowthOSError) as caught:
            PostgresStore(connection).load_targets("2026-10")
        self.assertEqual(caught.exception.code, "READ_ONLY_ROLE")

    def test_database_settings_require_explicit_verified_tls(self):
        validate_reader_settings({"host": "db.invalid", "sslmode": "verify-full"})
        for settings in ({}, {"host": "db.invalid", "sslmode": "require"},
                         {"host": "a.invalid,b.invalid", "sslmode": "verify-full"}):
            with self.assertRaises(GrowthOSError):
                validate_reader_settings(settings)


class OwnerSettingsTests(unittest.TestCase):
    def test_all_database_settings_use_one_env_file_and_only_owner_connects(self):
        driver, conninfo = ModuleType("psycopg"), ModuleType("psycopg.conninfo")
        driver.connect = MagicMock()
        configs = {name: {"host": "db.invalid", "dbname": "growthos", "sslmode": "verify-full", "user": name}
                   for name in ("owner", "writer", "reader")}
        conninfo.conninfo_to_dict = lambda value: configs[value]
        migration = MagicMock(return_value={"migrated": True})
        with patch.dict(sys.modules, {"psycopg": driver, "psycopg.conninfo": conninfo}), \
                patch("gwosdata.migrate_monthly.local_setting", side_effect=["owner", "writer", "reader"]) as setting:
            self.assertEqual(run(migration), {"migrated": True})
        root = Path(__file__).resolve().parents[1]
        self.assertEqual([call.args for call in setting.call_args_list], [
            ("GROWTHOS_OWNER_DSN", root / ".env"),
            ("GROWTHOS_WRITER_DSN", root / ".env"),
            ("GROWTHOS_READER_DSN", root / ".env"),
        ])
        driver.connect.assert_called_once()
        self.assertEqual(driver.connect.call_args.kwargs["user"], "owner")
        self.assertEqual(migration.call_args.args[1:], ("writer", "reader"))

    def test_distinct_logins_and_same_endpoint_remain_required_before_connection(self):
        for fault in ("same_login", "other_endpoint"):
            driver, conninfo = ModuleType("psycopg"), ModuleType("psycopg.conninfo")
            driver.connect = MagicMock()
            configs = {name: {"host": "db.invalid", "dbname": "growthos", "sslmode": "verify-full", "user": name}
                       for name in ("owner", "writer", "reader")}
            configs["reader"]["user" if fault == "same_login" else "host"] = "owner" if fault == "same_login" else "other.invalid"
            conninfo.conninfo_to_dict = lambda value: configs[value]
            with patch.dict(sys.modules, {"psycopg": driver, "psycopg.conninfo": conninfo}), \
                    patch("gwosdata.migrate_monthly.local_setting", side_effect=["owner", "writer", "reader"]), \
                    self.subTest(fault=fault), self.assertRaises(GrowthOSError):
                run(MagicMock())
            driver.connect.assert_not_called()
