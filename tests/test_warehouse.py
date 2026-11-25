import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from types import SimpleNamespace
from importlib.util import find_spec
from threading import Event
from unittest.mock import patch

from gwosdata.errors import GrowthOSError
from gwosdata.warehouse_cli import WarehouseExtractor, extraction_environment, fetch_data, main
from gwosdata.postgres import local_setting
from gwosdata.warehouse_client import (
    BIKE_SERVICES, MonthWindow, Operation, WarehouseConfig, WarehouseSession,
    _default_connection, operator_environment, query_for, read_operator_file, show_authorization_url,
)


class OperatorTerminal(io.StringIO):
    def isatty(self):
        return True


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.operation = None
        self.cancelled = Event()
        self.closed = Event()
        self.release = Event()

    def execute(self, sql, params=()):
        self.connection.executed_calls.append((sql, tuple(params)))
        entry = self.connection.lookup.get((sql, tuple(params)))
        if entry is None:
            entry = self.connection.rendered_lookup[(sql, tuple(params))]
        self.operation, self.window = entry
        self.connection.operations.append(self.operation)
        hook = self.connection.hooks.get(self.operation)
        if hook:
            hook(self)
        spec = query_for(self.operation, self.window)
        columns = self.connection.columns.get(self.operation, spec.columns)
        self.description = [(name, None) for name in columns]

    def fetchall(self):
        self.connection.fetches.append((self.operation, None))
        if (self.operation, self.window.value) in self.connection.month_rows:
            return self.connection.month_rows[(self.operation, self.window.value)]
        return self.connection.rows[self.operation]

    def cancel(self):
        self.cancelled.set()
        self.release.set()

    def close(self):
        self.closed.set()


class FakeConnection:
    def __init__(self, window, extra_windows=()):
        self.window = window
        self.windows = (window, *extra_windows)
        self.rows = {
            Operation.AUTHORIZE: [(1,)],
            Operation.INSTALLS_MONTHLY: [("delhi", "Delhi", 100), ("mystery_town", "Mystery Town", 20)],
            Operation.REGISTRATIONS_MONTHLY: [("delhi", "Delhi", 60), ("mystery_town", "Mystery Town", 10)],
            Operation.FTU_MONTHLY: [("delhi", "Delhi", 40, 20, 12, 6)],
            Operation.FUNNEL_MONTHLY: [("delhi", "Delhi", 1000, 800, 700, 300, 250, 200, 180, 150, 140)],
        }
        self.month_rows = {}
        self.lookup = {
            (query_for(op, w).sql, query_for(op, w).parameters): (op, w)
            for w in self.windows for op in Operation
        }
        self.rendered_lookup = {
            (query_for(op, w).render(), ()): (op, w)
            for w in self.windows for op in Operation
        }
        self.operations, self.fetches, self.cursors, self.executed_calls = [], [], [], []
        self.columns, self.hooks = {}, {}
        self.closed = Event()

    def cursor(self):
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed.set()
        for cursor in self.cursors:
            cursor.release.set()


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.window = MonthWindow("2026-09")
        self.config = WarehouseConfig("warehouse.example.invalid", "operator@example.invalid")
        self.connection = FakeConnection(self.window)
        self.session = WarehouseSession(self.config, connection_factory=lambda *_: self.connection)
        self.addCleanup(self.session.close)
        file_patch = patch("gwosdata.warehouse_client.read_operator_file", return_value={})
        self.file_reader = file_patch.start()
        self.addCleanup(file_patch.stop)

    def invoke(self, args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(args)
        return code, json.loads(out.getvalue())

    def test_monthly_queries_and_month_window_boundaries(self):
        self.assertEqual(self.window.day_parameters, ("20260901", "20261001"))
        self.assertEqual(BIKE_SERVICES, ("link",))
        with self.assertRaises(GrowthOSError):
            MonthWindow("9999-01")
        installs = query_for(Operation.INSTALLS_MONTHLY, self.window)
        self.assertIn("WHEN 'guntur' THEN 'vijayawada'", installs.sql)
        for operation in (Operation.INSTALLS_MONTHLY, Operation.REGISTRATIONS_MONTHLY,
                          Operation.FTU_MONTHLY, Operation.FUNNEL_MONTHLY):
            self.assertIn("WHEN 'unrecognized_city' THEN 'city_not_found'",
                          query_for(operation, self.window).sql)
        ftu = query_for(Operation.FTU_MONTHLY, self.window)
        self.assertIn("service_key = 'link'", ftu.sql)
        self.assertNotIn("'bike lite'", ftu.sql)
        self.assertNotIn("customer_id", ftu.columns)
        funnel = query_for(Operation.FUNNEL_MONTHLY, self.window)
        self.assertIn("Bike Rated by Women", funnel.sql)
        self.assertIn("Bike Rated by Women", funnel.sql.split("AS bike_net")[0])
        self.assertIn("customer_internal.customer_first_ride_lookup_immutable", funnel.sql)
        self.assertIn("GROUP BY customer_id", funnel.sql)
        self.assertIn("rf.time_value <= ltr_data.time_value", funnel.sql)
        self.assertIn("OR ltr_data.time_value IS NULL", funnel.sql)
        self.assertEqual(funnel.columns, (
            "canonical_city_key", "sample_source_city", "fe_sessions", "rr", "net",
            "bike_rr", "bike_net", "auto_rr", "auto_net", "cab_rr", "cab_net",
        ))

    def test_dotenv_and_operator_environment_config(self):
        with tempfile.TemporaryDirectory(prefix="growthos-dotenv-", dir="/tmp/opencode") as directory:
            path = Path(directory) / ".env"
            path.write_text('TRINO_HOST="file-warehouse.example.invalid"\nTRINO_USER=file-user\n'
                            'GROWTHOS_OWNER_DSN=private-owner\nGROWTHOS_WRITER_DSN=private-writer\n'
                            'GROWTHOS_READER_DSN=private-reader\n')
            parsed = read_operator_file(path)
            self.assertEqual(parsed["GROWTHOS_TRINO_HOST"], "file-warehouse.example.invalid")
            self.assertEqual(parsed["GROWTHOS_TRINO_USER"], "file-user")
            self.assertEqual(set(parsed), {"GROWTHOS_TRINO_HOST", "GROWTHOS_TRINO_USER"})
            with patch("gwosdata.warehouse_client.read_operator_file", wraps=read_operator_file):
                cfg = operator_environment({}, env_file=path)
            self.assertEqual(cfg.host, "file-warehouse.example.invalid")
            self.assertEqual(cfg.user, "file-user")
        with self.assertRaises(GrowthOSError):
            operator_environment({"GROWTHOS_TRINO_HOST": "https://bad.invalid", "GROWTHOS_TRINO_USER": "u"})

    def test_session_auth_schema_validation_and_timeout_safety(self):
        out, terminal = io.StringIO(), OperatorTerminal()
        url = "https://login.example.invalid/?temporary=supersecret"
        with redirect_stdout(out), redirect_stderr(terminal), patch("gwosdata.warehouse_client.sys.stdin.isatty", return_value=True), patch("webbrowser.open") as browser:
            show_authorization_url(url)
        browser.assert_not_called()
        self.assertIn(url, terminal.getvalue())

        self.session.authorize(self.window)
        self.session.run(Operation.INSTALLS_MONTHLY, self.window)
        for sql, params in self.connection.executed_calls:
            self.assertEqual(params, ())
            self.assertNotIn("?", sql)

        self.connection.columns[Operation.FTU_MONTHLY] = ("private_mobile", "sample_source_city", "platform_ftu")
        with self.assertRaises(GrowthOSError) as caught:
            self.session.run(Operation.FTU_MONTHLY, self.window)
        self.assertEqual(caught.exception.code, "WAREHOUSE_SCHEMA")

        timed = WarehouseSession(replace(self.config, query_timeout=0.04), connection_factory=lambda *_: FakeConnection(self.window))
        timed.authorize(self.window)
        timed._connection.hooks[Operation.INSTALLS_MONTHLY] = lambda cursor: cursor.release.wait(2)
        with self.assertRaises(GrowthOSError) as caught:
            timed.run(Operation.INSTALLS_MONTHLY, self.window)
        self.assertEqual(caught.exception.code, "WAREHOUSE_TIMEOUT")

    def test_two_month_extractor_and_immediate_publication(self):
        w9, w10 = MonthWindow("2026-09"), MonthWindow("2026-10")
        connection = FakeConnection(w9, extra_windows=(w10,))
        connection.month_rows[(Operation.INSTALLS_MONTHLY, "2026-10")] = [("delhi", "Delhi", 150), ("mystery_town", "Mystery Town", 30)]
        connection.month_rows[(Operation.REGISTRATIONS_MONTHLY, "2026-10")] = [("delhi", "Delhi", 90), ("mystery_town", "Mystery Town", 15)]
        connection.month_rows[(Operation.FTU_MONTHLY, "2026-10")] = [("delhi", "Delhi", 60, 30, 18, 9)]

        plan_code, plan_out = self.invoke(["--month", "2026-09", "--end-month", "2026-10"])
        self.assertEqual(plan_code, 0)
        self.assertFalse(plan_out["network_attempted"])
        self.assertEqual(len(plan_out["queries"]), 8)
        with WarehouseSession(self.config, connection_factory=lambda *_: connection) as session:
            dataset = WarehouseExtractor(session).extract(["2026-09", "2026-10"])
        self.assertEqual(len(dataset.data["facts"]), 4)
        self.assertEqual(sum(r["installs"] for r in dataset.data["facts"] if r["month"] == "2026-10"), 180)
        self.assertNotIn("coverage", dataset.data)
        self.assertNotIn("cutoff", dataset.data["month_metadata"][0]["source_reads"][0])
        with patch("gwosdata.warehouse_cli.sys.stdin.isatty", return_value=True), patch("gwosdata.warehouse_cli.sys.stderr.isatty", return_value=True), patch("gwosdata.warehouse_cli.fetch_data", return_value=dataset), patch("gwosdata.warehouse_cli.connect_writer") as writer:
            writer.return_value.__enter__.return_value.replace_months.return_value = dataset
            code, output = self.invoke(["--month", "2026-09", "--end-month", "2026-10", "--authorize-live"])
        self.assertEqual(code, 0)
        self.assertTrue(output["published"])
        self.assertEqual(output["replaced_months"], ["2026-09", "2026-10"])
        writer.return_value.__enter__.return_value.replace_months.assert_called_once_with(dataset)

    def test_fetch_failure_never_opens_writer_and_child_environment_is_clean(self):
        env = extraction_environment({"HOME": "/tmp/opencode", "PATH": "/usr/bin",
            "GROWTHOS_WRITER_DSN": "secret", "GROWTHOS_READER_DSN": "secret",
            "GROWTHOS_OWNER_DSN": "secret", "NEON_API_KEY": "secret", "GROWTHOS_TRINO_HOST": "host.invalid"})
        self.assertEqual(set(env), {"HOME", "PATH", "GROWTHOS_TRINO_HOST"})
        with patch("gwosdata.warehouse_cli.sys.stdin.isatty", return_value=True), patch("gwosdata.warehouse_cli.sys.stderr.isatty", return_value=True), patch("gwosdata.warehouse_cli.fetch_data", side_effect=GrowthOSError("WAREHOUSE_QUERY", "sanitized")), patch("gwosdata.warehouse_cli.connect_writer") as writer:
            code, output = self.invoke(["--month", "2026-09", "--authorize-live"])
        self.assertEqual(code, 2)
        self.assertEqual(output["error"]["code"], "WAREHOUSE_QUERY")
        writer.assert_not_called()

    def test_empty_required_source_rejects_the_entire_range(self):
        self.connection.rows[Operation.FTU_MONTHLY] = []
        with self.assertRaises(GrowthOSError) as caught:
            WarehouseExtractor(self.session).extract(["2026-09"])
        self.assertEqual(caught.exception.code, "COVERAGE")

    def test_private_aggregate_pipe_is_bounded_and_sanitizes_failures(self):
        from fixtures import demo_payload
        from gwosdata.contracts import validate_data
        raw = demo_payload()
        with patch("gwosdata.warehouse_cli.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(raw).encode())) as run:
            self.assertEqual(fetch_data(raw["months"]), validate_data(raw))
            settings = run.call_args.kwargs
            self.assertNotIn("GROWTHOS_WRITER_DSN", settings["env"])
            self.assertNotIn("stderr", settings)  # OAuth remains on the operator terminal.
            self.assertNotIn("stdin", settings)
        with patch("gwosdata.warehouse_cli.subprocess.run", return_value=SimpleNamespace(returncode=2, stdout=b'{"error":{"code":"private-secret","message":"secret"}}')):
            with self.assertRaises(GrowthOSError) as caught:
                fetch_data(raw["months"])
            self.assertNotIn("secret", str(caught.exception))
        with patch("gwosdata.warehouse_cli.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(raw).encode())), patch("gwosdata.warehouse_cli.MAX_TRANSFER_BYTES", 1):
            with self.assertRaises(GrowthOSError):
                fetch_data(raw["months"])
        with patch("gwosdata.warehouse_cli.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(raw).encode())):
            with self.assertRaises(GrowthOSError):
                fetch_data(["2026-09"])

    def test_writer_setting_loader_selects_one_literal_without_environment_mutation(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / '.env'
            path.write_text("GROWTHOS_TRINO_HOST=host.invalid\nGROWTHOS_WRITER_DSN='literal-value'\n")
            self.assertEqual(local_setting("GROWTHOS_WRITER_DSN", path), "literal-value")
            self.assertNotIn("GROWTHOS_WRITER_DSN", os.environ)
            path.write_text("GROWTHOS_WRITER_DSN=one\nGROWTHOS_WRITER_DSN=two\n")
            with self.assertRaises(GrowthOSError):
                local_setting("GROWTHOS_WRITER_DSN", path)
            path.unlink()
            path.symlink_to(Path(directory) / 'not-read')
            with self.assertRaises(GrowthOSError):
                local_setting("GROWTHOS_WRITER_DSN", path)

    @unittest.skipUnless(find_spec("trino") is not None, "Optional pinned Trino dependency not installed")
    def test_pinned_trino_client_construction(self):
        import trino.auth
        previous_disable = logging.root.manager.disable
        self.addCleanup(logging.disable, previous_disable)
        with patch("trino.dbapi.connect") as connect:
            _default_connection(self.config, lambda url: None)
        settings = connect.call_args.kwargs
        self.assertEqual(settings["http_scheme"], "https")
        self.assertIs(settings["verify"], True)
        self.assertEqual(settings["request_timeout"], 30)
        for forbidden in ("source", "client_tags", "timezone", "session_properties"):
            self.assertNotIn(forbidden, settings)
        self.assertIs(type(settings["auth"]._bearer._token_cache), trino.auth._OAuth2TokenInMemoryCache)


if __name__ == "__main__":
    unittest.main()
