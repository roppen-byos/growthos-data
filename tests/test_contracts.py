import copy
import unittest

from fixtures import demo_payload
from gwosdata.classification import resolve_city
from gwosdata.contracts import FACT_FIELDS, FUNNEL_VOLUMES, checksum, month_range, validate_data
from gwosdata.errors import GrowthOSError
from gwosdata.import_targets import parse_csv
from gwosdata.targets import target_city_review
from test_import_targets import SOURCE


class ContractTests(unittest.TestCase):
    def test_historic_checksum_and_detached_normalization(self):
        raw = demo_payload()
        result = validate_data(raw)
        self.assertEqual(result.result_checksum, "d7eba0cdd0a82be07c1328401f0399e469555e224698bb8b92db5acbc471add2")
        raw["facts"].reverse()
        self.assertEqual(validate_data(raw), result)
        raw["facts"][0]["installs"] = 999
        self.assertNotEqual(validate_data(raw), result)
        detached = result.data
        detached["facts"].clear()
        self.assertTrue(result.data["facts"])

    def test_funnel_counts_do_not_change_historic_checksum_but_require_provenance(self):
        raw = demo_payload()
        before = checksum(raw["facts"])
        raw["facts"][0]["fe_sessions"] = 12
        self.assertEqual(checksum(raw["facts"]), before)
        meta = raw["month_metadata"][0]
        meta["source_reads"] = [r for r in meta["source_reads"] if r["source"] != "funnel"]
        with self.assertRaises(GrowthOSError) as caught:
            validate_data(raw)
        self.assertEqual(caught.exception.code, "PROVENANCE")
        meta["source_reads"].append({**meta["source_reads"][0], "source": "funnel"})
        self.assertEqual(validate_data(raw).data["facts"][0]["fe_sessions"], 12)

    def test_invalid_counts_grain_fields_and_provenance_fail_closed(self):
        for invalid in (-1, True, 2**63, 1.5):
            raw = demo_payload()
            raw["facts"][0]["installs"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(GrowthOSError):
                validate_data(raw)
        for fault in ("duplicate", "missing_field", "unknown_field", "bad_tier", "missing_read"):
            raw = demo_payload()
            if fault == "duplicate":
                raw["facts"].append(copy.deepcopy(raw["facts"][0]))
            elif fault == "missing_field":
                del raw["facts"][0]["installs"]
            elif fault == "unknown_field":
                raw["facts"][0]["raw_customer"] = "withheld"
            elif fault == "bad_tier":
                raw["facts"][0]["tier"] = "T4"
            else:
                raw["month_metadata"][0]["source_reads"].pop()
            with self.subTest(fault=fault), self.assertRaises(GrowthOSError):
                validate_data(raw)

    def test_null_metrics_and_month_bounds_are_preserved(self):
        raw = demo_payload()
        raw["facts"][0]["installs"] = None
        raw["facts"][0].update({m: None for m in FUNNEL_VOLUMES})
        row = validate_data(raw).data["facts"][0]
        self.assertIsNone(row["installs"])
        self.assertTrue(all(row[m] is None for m in FUNNEL_VOLUMES))
        self.assertEqual(set(row), FACT_FIELDS)
        self.assertEqual(month_range("2025-12", "2026-01"), ["2025-12", "2026-01"])
        for start, end in (("2026-02", "2026-01"), ("2025-01", "2026-01")):
            with self.assertRaises(GrowthOSError):
                month_range(start, end)

    def test_city_merges_tiers_and_unattributed_storage_match_existing_contract(self):
        for source, expected in (("Faridabad", "delhi"), ("Attingal", "trivandrum"), ("Guntur", "vijayawada")):
            self.assertEqual(resolve_city(source)[0], expected)
        self.assertEqual(resolve_city("Bangalore")[1:3], ("Bangalore", "T1"))
        self.assertEqual(resolve_city("Pune")[1:3], ("Pune", "T2"))
        self.assertEqual(resolve_city("new_town", "New Town"), ("new_town", "New Town", "T3", ["New Town"]))
        self.assertEqual(resolve_city("UNRECOGNIZED_CITY"), ("city_not_found", "city_not_found", "T3", ["city_not_found"]))

    def test_one_time_review_preserves_exact_threshold_and_missing_operands(self):
        plans = parse_csv(SOURCE, "platform_ftu")
        raw = demo_payload()
        row = next(r for r in raw["facts"] if r["month"] == "2026-10")
        row.update(city_key="trivandrum", city="Trivandrum", source_cities=["Trivandrum"])
        for actual, qualifies in ((114, False), (113, True)):
            row["platform_ftu"] = actual
            result = target_city_review(validate_data(raw), plans[1], "trivandrum")
            self.assertEqual(result["variance"], actual - 120)
            self.assertEqual(result["above_5_percent"], qualifies)
            self.assertEqual(result["target_checksum"], plans[1].result_checksum)
        result = target_city_review(validate_data(raw), plans[1], "missing_city")
        self.assertIsNone(result["variance"])
        self.assertEqual(result["unavailable_reason"], "missing_target_or_actual")
