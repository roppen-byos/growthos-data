"""Current-month extraction through the oldest landed source day."""

from datetime import date, datetime, timezone
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fixtures import demo_payload
from gwosdata.contracts import validate_data, validate_metadata
from gwosdata.errors import GrowthOSError
from gwosdata.warehouse_cli import WarehouseExtractor, fetch_data, request_windows
from gwosdata.warehouse_client import (
    ALL_OPERATIONS, LANDED_OPERATIONS, MonthWindow, Operation, WarehouseConfig, WarehouseSession,
    landed_cutoff, landed_day, query_for,
)
from test_warehouse import FakeConnection

TODAY = date(2026, 10, 15)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)  # 05:30 on 15 Oct in Asia/Kolkata


def landed(**overrides):
    latest = {"installs": "20261014", "registrations": "20261014",
              "ftu": "2026-10-14 23:58:01.000", "funnel": "2026-10-14", "first_ride_lookup": "2026-10-14"}
    latest.update(overrides)
    return latest


def stub_landed(connection, **overrides):
    for operation, source in LANDED_OPERATIONS.items():
        connection.rows[operation] = [(landed(**overrides)[source],)]


class MonthToDateTests(unittest.TestCase):
    def setUp(self):
        today = patch("gwosdata.warehouse_client.business_today", return_value=TODAY)
        today.start()
        self.addCleanup(today.stop)

    def test_window_accepts_only_current_month_before_today(self):
        window = MonthWindow("2026-10", date(2026, 10, 14))
        self.assertEqual(window.day_parameters, ("20261001", "20261015"))
        self.assertEqual(window.date_parameters, ("2026-10-01", "2026-10-15"))
        self.assertEqual(MonthWindow.month_to_date("2026-10").through, date(2026, 10, 14))
        self.assertEqual(MonthWindow("2026-09").end, date(2026, 10, 1))
        for value, through in (("2026-10", None), ("2026-10", TODAY), ("2026-10", date(2026, 9, 30)),
                               ("2026-09", date(2026, 9, 30)), ("2026-11", date(2026, 11, 1)),
                               ("2026-10", datetime(2026, 10, 14))):
            with self.subTest(value=value, through=through), self.assertRaises(GrowthOSError) as caught:
                MonthWindow(value, through)
            self.assertEqual(caught.exception.code, "INCOMPLETE_MONTH")

    def test_landed_probes_are_mtd_only_per_source_and_bounded_to_seven_days(self):
        self.assertFalse(set(LANDED_OPERATIONS) & set(ALL_OPERATIONS))
        probe = MonthWindow.month_to_date("2026-10")
        self.assertEqual(probe.lookback_start, date(2026, 10, 8))
        self.assertEqual(MonthWindow("2026-10", date(2026, 10, 3)).lookback_start, date(2026, 10, 1))
        expected = {Operation.LANDED_INSTALLS: ("20261008", "20261015"),
                    Operation.LANDED_REGISTRATIONS: ("20261008", "20261015"),
                    Operation.LANDED_FTU: ("2026-10-08", "2026-10-15"),
                    Operation.LANDED_FUNNEL: ("2026-10-08", "2026-10-15"),
                    Operation.LANDED_FIRST_RIDE_LOOKUP: ("2026-10-08", "2026-10-15")}
        for operation, params in expected.items():
            with self.subTest(operation=operation.value):
                with self.assertRaises(GrowthOSError):
                    query_for(operation, MonthWindow("2026-09"))
                spec = query_for(operation, probe)
                self.assertEqual(spec.parameters, params)
                self.assertEqual(spec.columns, ("latest",))
                self.assertEqual(spec.sql.count("FROM"), 1)
        self.assertIn("customer_first_ride_lookup_immutable", query_for(Operation.LANDED_FIRST_RIDE_LOOKUP, probe).sql)

    def test_cutoff_is_oldest_landed_day_and_missing_sources_reject(self):
        self.assertEqual(landed_day("20261014"), date(2026, 10, 14))
        self.assertEqual(landed_day("2026-10-14 23:58:01.000"), date(2026, 10, 14))
        for bad in (None, "", "2026-1-4", "20261340", "2026-10-140", 20261014):
            self.assertIsNone(landed_day(bad))
        probe = MonthWindow.month_to_date("2026-10")
        row = landed(funnel="2026-10-12")
        self.assertEqual(landed_cutoff(probe, row), date(2026, 10, 12))
        for name, value in (("installs", None), ("ftu", "2026-10-07"),
                            ("first_ride_lookup", "2026-10-15"), ("funnel", "garbage")):
            with self.subTest(name=name), self.assertRaises(GrowthOSError) as caught:
                landed_cutoff(probe, {**row, name: value})
            self.assertEqual(caught.exception.code, "COVERAGE")
            self.assertIn(name, str(caught.exception))
        with self.assertRaises(GrowthOSError):
            landed_cutoff(probe, {k: v for k, v in row.items() if k != "ftu"})
        day_two = MonthWindow("2026-10", date(2026, 10, 1))
        with self.assertRaises(GrowthOSError) as caught:
            landed_cutoff(day_two, {**landed(), "ftu": "2026-09-30"})
        self.assertIn("last 1 days", str(caught.exception))

    def test_extractor_cuts_current_month_at_landed_day(self):
        september, probe = MonthWindow("2026-09"), MonthWindow.month_to_date("2026-10")
        connection = FakeConnection(september, extra_windows=(probe,))
        connection.register(MonthWindow("2026-10", date(2026, 10, 12)))
        stub_landed(connection, registrations="20261012")
        config = WarehouseConfig("warehouse.example.invalid", "operator@example.invalid")
        with patch("gwosdata.warehouse_cli.datetime", FixedDatetime), \
                patch("gwosdata.warehouse_client.datetime", FixedDatetime), \
                WarehouseSession(config, connection_factory=lambda *_: connection) as session:
            dataset = WarehouseExtractor(session).extract(["2026-09", "2026-10"], month_to_date=True)
        metadata = {m["month"]: m for m in dataset.data["month_metadata"]}
        self.assertNotIn("cutoff_date", metadata["2026-09"])
        self.assertEqual(metadata["2026-10"]["cutoff_date"], "2026-10-12")
        october = [window for op, window in (connection.lookup[call] if call in connection.lookup
                   else connection.rendered_lookup[call] for call in connection.executed_calls)
                   if op == Operation.FTU_MONTHLY and window.value == "2026-10"]
        self.assertEqual([w.through for w in october], [date(2026, 10, 12)])
        self.assertEqual([op for op in connection.operations if op in LANDED_OPERATIONS], list(LANDED_OPERATIONS))

    def test_extraction_crossing_midnight_into_next_month_completes(self):
        # Starts 31 Oct (IST); the clock reaches 1 Nov while the landed-date probes run.
        class AfterMidnight(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 31, 19, 0, tzinfo=timezone.utc)  # 00:30 on 1 Nov in Asia/Kolkata

        with patch("gwosdata.warehouse_client.business_today", return_value=date(2026, 10, 31)) as today:
            probe = MonthWindow.month_to_date("2026-10")
            connection = FakeConnection(probe)
            connection.register(MonthWindow("2026-10", date(2026, 10, 30), as_of=date(2026, 10, 31)))
            stub_landed(connection, installs="20261030", registrations="20261030", ftu="2026-10-30",
                        funnel="2026-10-30", first_ride_lookup="2026-10-30")
            connection.hooks[Operation.LANDED_INSTALLS] = lambda cursor: setattr(today, "return_value", date(2026, 11, 1))
            config = WarehouseConfig("warehouse.example.invalid", "operator@example.invalid")
            with patch("gwosdata.warehouse_cli.datetime", AfterMidnight), \
                    patch("gwosdata.warehouse_client.datetime", AfterMidnight), \
                    WarehouseSession(config, connection_factory=lambda *_: connection) as session:
                dataset = WarehouseExtractor(session).extract(["2026-10"], month_to_date=True)
        self.assertEqual(dataset.data["month_metadata"][0]["cutoff_date"], "2026-10-30")

    def test_extraction_stops_before_monthly_queries_when_a_source_has_not_landed(self):
        probe = MonthWindow.month_to_date("2026-10")
        connection = FakeConnection(probe)
        stub_landed(connection, first_ride_lookup=None)
        config = WarehouseConfig("warehouse.example.invalid", "operator@example.invalid")
        with WarehouseSession(config, connection_factory=lambda *_: connection) as session:
            with self.assertRaises(GrowthOSError) as caught:
                WarehouseExtractor(session).extract(["2026-10"], month_to_date=True)
        self.assertEqual(caught.exception.code, "COVERAGE")
        self.assertNotIn(Operation.INSTALLS_MONTHLY, connection.operations)

    def test_month_to_date_requires_current_last_month(self):
        self.assertEqual([w.through for w in request_windows(["2026-09", "2026-10"], True)], [None, date(2026, 10, 14)])
        with self.assertRaises(GrowthOSError):
            request_windows(["2026-09"], True)
        with self.assertRaises(GrowthOSError):
            request_windows(["2026-10"], False)


class MonthToDateContractTests(unittest.TestCase):
    def meta(self, **extra):
        return {"month": "2026-10", "extracted_at": "2026-10-15T00:00:00+00:00",
                "business_timezone": "Asia/Kolkata", "source_reads": [
                    {"source": s, "month": "2026-10", "started_at": "2026-10-14T23:00:00+00:00",
                     "completed_at": "2026-10-14T23:10:00+00:00"} for s in ("installs", "registrations", "ftu", "funnel")],
                **extra}

    def test_cutoff_must_be_in_extraction_month_before_extraction_day(self):
        validate_metadata(self.meta(cutoff_date="2026-10-14"))
        validate_metadata(self.meta(cutoff_date="2026-10-01"))
        for cutoff, code in (("2026-10-15", "INCOMPLETE_MONTH"), ("2026-09-30", "INCOMPLETE_MONTH"),
                             ("2026-10-1", "DATE"), ("2026-02-30", "DATE"), (None, "DATE")):
            with self.subTest(cutoff=cutoff), self.assertRaises(GrowthOSError) as caught:
                validate_metadata(self.meta(cutoff_date=cutoff))
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(GrowthOSError) as caught:
            validate_metadata(self.meta())
        self.assertEqual(caught.exception.code, "INCOMPLETE_MONTH")
        with self.assertRaises(GrowthOSError) as caught:
            validate_metadata(self.meta(cutoff_date="2026-10-14", note="x"))
        self.assertEqual(caught.exception.code, "SCHEMA")

    def test_extraction_finishing_after_midnight_keeps_its_cutoff(self):
        # Started 31 Oct (cutoff 30 Oct), finished 1 Nov in Asia/Kolkata.
        late = {**self.meta(cutoff_date="2026-10-30"), "extracted_at": "2026-10-31T18:40:00+00:00"}
        validate_metadata(late, now=datetime(2026, 11, 2, tzinfo=timezone.utc))
        with self.assertRaises(GrowthOSError):
            validate_metadata({**late, "cutoff_date": "2026-10-31"}, now=datetime(2026, 11, 2, tzinfo=timezone.utc))

    def test_ended_month_cannot_carry_cutoff(self):
        ended = {**self.meta(cutoff_date="2026-09-30"), "month": "2026-09"}
        with self.assertRaises(GrowthOSError) as caught:
            validate_metadata(ended)
        self.assertEqual(caught.exception.code, "INCOMPLETE_MONTH")
        # An ended month cut before its last day stays valid; readers flag it as awaiting a complete refresh.
        reads = [{**read, "month": "2026-09"} for read in ended["source_reads"]]
        validate_metadata({**ended, "cutoff_date": "2026-09-29", "source_reads": reads})

    def test_fetch_rejects_cutoffs_that_differ_from_request(self):
        raw = demo_payload()
        partial = copy.deepcopy(raw)
        partial["month_metadata"][-1]["month"] = "2026-10"
        with patch("gwosdata.warehouse_cli.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout=json.dumps(raw).encode())) as run:
            with self.assertRaises(GrowthOSError) as caught:
                fetch_data(raw["months"], month_to_date=True)
            self.assertEqual(caught.exception.code, "REQUEST_MISMATCH")
            self.assertIn("--month-to-date", run.call_args.args[0])
        validate_data(raw)


    def test_parent_relays_only_fixed_shape_timeout_and_coverage_detail(self):
        def child(code, message):
            body = json.dumps({"error": {"code": code, "message": message}}).encode()
            return patch("gwosdata.warehouse_cli.subprocess.run", return_value=SimpleNamespace(returncode=2, stdout=body))
        for code, message in (("WAREHOUSE_TIMEOUT", "Warehouse landed-ftu exceeded its time budget."),
                              ("COVERAGE", "No landed data in the last 7 days for: first_ride_lookup; nothing was published.")):
            with self.subTest(code=code), child(code, message), self.assertRaises(GrowthOSError) as caught:
                fetch_data(["2026-09"])
            self.assertEqual(caught.exception.code, code)
            self.assertIn(message, str(caught.exception))
        with child("WAREHOUSE_TIMEOUT", "Warehouse secret-host.example exceeded its time budget. token=abc"), \
                self.assertRaises(GrowthOSError) as caught:
            fetch_data(["2026-09"])
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
