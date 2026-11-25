import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from growthos.contracts import FACT_FIELDS, validate_data
from growthos.errors import GrowthOSError

from gwosdata.migrate_city_sentinel import check_tables, main, merge_facts, migrate_connection


def _fact(key, city, tier, **metrics):
    row = {field: None for field in FACT_FIELDS}
    row.update(month="2026-10", city_key=key, city=city, tier=tier, source_cities=[city])
    row.update(metrics)
    return row


def _dataset(facts):
    return {
        "nonproduction": False, "months": ["2026-10"],
        "month_metadata": [{
            "month": "2026-10", "extracted_at": "2026-11-02T00:01:00+00:00",
            "business_timezone": "Asia/Kolkata",
            "source_reads": [
                {"source": source, "month": "2026-10",
                 "started_at": f"2026-11-02T00:00:{i * 10:02d}+00:00",
                 "completed_at": f"2026-11-02T00:00:{i * 10 + 5:02d}+00:00"}
                for i, source in enumerate(("installs", "registrations", "ftu", "funnel"))
            ],
        }],
        "facts": facts,
    }


class CitySentinelMergeTests(unittest.TestCase):
    def test_both_sentinels_merge_into_one_row_preserving_counts(self):
        facts = [
            _fact("city_not_found", "city_not_found", "T3", installs=240),
            _fact("unrecognized_city", "UNRECOGNIZED_CITY", "T3", registrations=600),
            _fact("harbor", "Harbor", "T1", installs=10, registrations=5, platform_ftu=4),
        ]
        merged, folded = merge_facts(facts)
        self.assertEqual(folded, 1)
        self.assertEqual(sorted(row["city_key"] for row in merged),
                         ["city_not_found", "harbor"])
        target = next(row for row in merged if row["city_key"] == "city_not_found")
        self.assertEqual((target["installs"], target["registrations"]), (240, 600))
        self.assertEqual((target["city"], target["tier"]), ("city_not_found", "T3"))
        self.assertEqual(target["source_cities"], ["city_not_found"])
        result = validate_data(_dataset(merged))
        self.assertEqual(result.data["facts"], merged)

    def test_metrics_from_both_rows_are_summed_when_both_present(self):
        facts = [
            _fact("city_not_found", "city_not_found", "T3", installs=240, fe_sessions=7),
            _fact("unrecognized_city", "UNRECOGNIZED_CITY", "T3", registrations=600, fe_sessions=3),
        ]
        merged, _ = merge_facts(facts)
        target = merged[0]
        self.assertEqual((target["installs"], target["registrations"]), (240, 600))
        self.assertEqual(target["fe_sessions"], 10)

    def test_lone_unrecognized_row_is_renamed_without_data_loss(self):
        facts = [_fact("unrecognized_city", "UNRECOGNIZED_CITY", "T3", registrations=600)]
        merged, folded = merge_facts(facts)
        self.assertEqual(folded, 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["city_key"], "city_not_found")
        self.assertEqual(merged[0]["registrations"], 600)
        self.assertEqual(merged[0]["tier"], "T3")

    def test_month_without_the_old_sentinel_is_unchanged(self):
        facts = [_fact("harbor", "Harbor", "T1", installs=10)]
        merged, folded = merge_facts(facts)
        self.assertEqual((merged, folded), (facts, 0))

    def test_table_guard_accepts_installed_targets_and_rejects_unknown_state(self):
        check_tables({"facts", "month_metadata"})
        check_tables({"facts", "month_metadata", "targets", "target_imports"})
        for names in ({"facts"}, {"month_metadata", "targets"}, {"facts", "month_metadata", "extra"},
                      {"facts", "month_metadata", "targets"}):
            with self.assertRaises(GrowthOSError) as caught:
                check_tables(names)
            self.assertEqual(caught.exception.code, "MIGRATION_STATE")

    def test_explicit_authorization_is_required_before_loading_owner_settings(self):
        with patch("gwosdata.migrate_city_sentinel.run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 2)
            run.assert_not_called()
            run.return_value = {"migrated": True}
            self.assertEqual(main(["--authorize-migration"]), 0)
            run.assert_called_once_with(migrate_connection)


if __name__ == "__main__":
    unittest.main()