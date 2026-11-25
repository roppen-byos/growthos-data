import io
from pathlib import Path
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from gwosdata.errors import GrowthOSError
from gwosdata.import_targets import (
    import_connection, main, parse_csv, _plan_values, _stored_values,
)


SOURCE = b"""city,Tag,LF-Tag,2026-09,2026-10
Ludhiana,T2,T2,100,110
Ludhiana,T3,Stable,999,999
Attingal,T3,Seeding,30,40
Trivandrum,T3,Stable,70,80
New launch,T3,New Launch,0,50
Grand Total,,,300,400
"""


def by_month(plans):
    return {p.data["month"]: p for p in plans}


class TargetImportTests(unittest.TestCase):
    def invoke(self, args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args)
        return code, json.loads(output.getvalue())

    def test_approved_ludhiana_selection_merge_and_separate_overall_and_fallback(self):
        plans = parse_csv(SOURCE, "platform_ftu")
        self.assertEqual([p.data["month"] for p in plans], ["2026-09", "2026-10"])
        for plan, target, merged in zip(plans, (100, 110), (100, 120)):
            data = plan.data
            cities = {r["city_key"]: r for r in data["rows"] if r["kind"] == "city"}
            self.assertEqual(cities["ludhiana"]["platform_ftu_target"], target)
            self.assertEqual(cities["trivandrum"]["platform_ftu_target"], merged)
            self.assertIsNone(cities["ludhiana"]["registrations_target"])
            self.assertNotIn("attingal", cities)
            self.assertEqual(len(data["rows"]), 4)
        reversed_rows = SOURCE.replace(
            b"Ludhiana,T2,T2,100,110\nLudhiana,T3,Stable,999,999",
            b"Ludhiana,T3,Stable,999,999\nLudhiana,T2,T2,100,110")
        self.assertEqual(parse_csv(reversed_rows, "platform_ftu")[1].result_checksum, plans[1].result_checksum)

    def test_metric_selects_only_its_own_column(self):
        plans = parse_csv(SOURCE, "registrations")
        for row in plans[0].data["rows"]:
            self.assertIsNone(row["platform_ftu_target"])
        cities = {r["city_key"]: r for r in plans[0].data["rows"] if r["kind"] == "city"}
        self.assertEqual(cities["ludhiana"]["registrations_target"], 100)
        self.assertEqual(cities["trivandrum"]["registrations_target"], 100)
        with self.assertRaises(GrowthOSError):
            parse_csv(SOURCE, "installs")

    def test_month_normalization_blanks_and_zero_are_preserved(self):
        source = SOURCE.replace(b"2026-09,2026-10", b"2027-2,2027-4").replace(b"100,110", b",0")
        plans = parse_csv(source, "registrations")
        self.assertEqual([p.data["month"] for p in plans], ["2027-02", "2027-04"])
        rows = [next(r for r in p.data["rows"] if r["city_key"] == "ludhiana") for p in plans]
        self.assertIsNone(rows[0]["registrations_target"])
        self.assertEqual(rows[1]["registrations_target"], 0)

    def test_bad_headers_duplicates_counts_and_unapproved_merges_are_rejected(self):
        sources = [
            SOURCE.replace(b"2026-09,2026-10", b"2026-9,2026-09"),
            SOURCE.replace(b"2026-09", b"2026-13"),
            SOURCE.replace(b"city,Tag", b"name,Tag"),
            SOURCE + b"Ludhiana,T2,T2,1,1\n",
            SOURCE + b"Trivandrum,T3,Stable,1,1\n",
            SOURCE + b"Delhi,T1,T1,1,1\nFaridabad,T3,Stable,2,2\n",
            SOURCE.replace(b"100,110", b"-1,110"),
            SOURCE.replace(b"100,110", b"1.5,110"),
            SOURCE.replace(b"100,110", b"9223372036854775808,110"),
            SOURCE.replace(b"Ludhiana,T2,T2,100,110\n", b""),
            SOURCE.replace(b"Grand Total,,,300,400\n", b""),
            SOURCE.replace(b"Attingal,T3,Seeding,30,40", b"Attingal,T3,Seeding,30"),
        ]
        for idx, source in enumerate(sources):
            with self.subTest(idx=idx), self.assertRaises(GrowthOSError):
                parse_csv(source, "platform_ftu")

    def test_authorization_and_source_validation_precede_owner_settings(self):
        with patch("gwosdata.import_targets.run") as owner, patch("gwosdata.import_targets.load_csv") as load:
            code, result = self.invoke(["--csv", "PRIVATE_path.csv", "--metric", "platform_ftu"])
            self.assertEqual(code, 2)
            self.assertNotIn("PRIVATE", str(result))
            owner.assert_not_called()
            load.assert_not_called()
        with patch("gwosdata.import_targets.run") as owner, patch("gwosdata.import_targets.load_csv") as load:
            code, _ = self.invoke(["--csv", "targets.csv", "--check-only"])
            self.assertEqual(code, 2)
            load.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.csv"
            path.write_bytes(SOURCE)
            with patch("gwosdata.import_targets.run") as owner:
                code, result = self.invoke(["--csv", str(path), "--metric", "registrations", "--check-only"])
                self.assertEqual(code, 0)
                self.assertEqual(result["metric"], "registrations")
                self.assertEqual(len(result["months"]), 2)
                owner.assert_not_called()
                path.write_bytes(b"invalid")
                self.assertEqual(self.invoke(["--csv", str(path), "--metric", "platform_ftu", "--authorize-import"])[0], 2)
                owner.assert_not_called()

    def test_existing_exact_metric_is_noop_and_conflicts_never_write(self):
        plans = parse_csv(SOURCE, "platform_ftu")
        expected = by_month(plans)
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = ("irf.targets",)
        with patch("gwosdata.import_targets._stored_months", return_value=["2026-09", "2026-10"]), \
                patch("gwosdata.import_targets._stored_values",
                      side_effect=lambda conn, value, metric: _plan_values(expected[value], metric)), \
                patch("gwosdata.import_targets._grant_reader") as grants:
            result = import_connection(connection, "writer", "reader", plans, "platform_ftu")
        self.assertFalse(result["imported"])
        self.assertEqual(result["metric"], "platform_ftu")
        self.assertNotIn("merged_city_review", result)
        grants.assert_called_once()
        self.assertTrue(all("INSERT" not in str(call) and "CREATE TABLE" not in str(call)
                            for call in connection.execute.call_args_list))
        connection.cursor.assert_not_called()  # No historical actuals read for an import review.
        with patch("gwosdata.import_targets._stored_months", return_value=["2026-09"]), \
                patch("gwosdata.import_targets._grant_reader") as grants:
            with self.assertRaises(GrowthOSError) as caught:
                import_connection(connection, "writer", "reader", plans, "platform_ftu")
        self.assertEqual(caught.exception.code, "MIGRATION_STATE")
        grants.assert_not_called()
        with patch("gwosdata.import_targets._stored_months", return_value=["2026-09", "2026-10"]), \
                patch("gwosdata.import_targets._stored_values", return_value={}), \
                patch("gwosdata.import_targets._grant_reader") as grants:
            with self.assertRaises(GrowthOSError) as caught:
                import_connection(connection, "writer", "reader", plans, "platform_ftu")
        self.assertEqual(caught.exception.code, "MIGRATION_STATE")
        grants.assert_not_called()

    def test_first_import_creates_schema_and_writes_only_its_metric(self):
        plans = parse_csv(SOURCE, "registrations")
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (None,)
        stored = {}
        def fake_values(conn, value, metric):
            return _plan_values(by_month(plans)[value], metric)
        with patch("gwosdata.import_targets._stored_values", side_effect=fake_values), \
                patch("gwosdata.import_targets._grant_reader") as grants:
            result = import_connection(connection, "writer", "reader", plans, "registrations")
        self.assertTrue(result["imported"])
        self.assertNotIn("merged_city_review", result)
        grants.assert_called_once()
        statements = [str(call) for call in connection.execute.call_args_list]
        self.assertTrue(any("CREATE TABLE irf.targets" in s for s in statements))
        cursor = connection.cursor.return_value.__enter__.return_value
        inserts = [c for c in cursor.executemany.call_args_list if "INSERT INTO irf.targets" in str(c)]
        self.assertTrue(inserts)
        for call in inserts:
            self.assertIn("registrations_target", str(call))
            self.assertNotIn("platform_ftu_target", str(call))
            self.assertEqual([row[-1] for row in call.args[1]], [100, 100, 0, 300] if call.args[1][0][0].month == 9 else [110, 120, 50, 400])

    def test_failed_import_verification_rolls_back_before_grants(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (None,)
        with patch("gwosdata.import_targets._stored_values", return_value={}), \
                patch("gwosdata.import_targets._grant_reader") as grants:
            with self.assertRaises(GrowthOSError) as caught:
                import_connection(connection, "writer", "reader", parse_csv(SOURCE, "platform_ftu"), "platform_ftu")
        self.assertEqual(caught.exception.code, "STORE_INTEGRITY")
        self.assertIsNotNone(connection.transaction.return_value.__exit__.call_args.args[0])
        grants.assert_not_called()

    def test_import_rejects_unsupported_metric(self):
        with self.assertRaises(GrowthOSError):
            import_connection(MagicMock(), "writer", "reader", parse_csv(SOURCE, "platform_ftu"), "installs")


if __name__ == "__main__":
    unittest.main()
