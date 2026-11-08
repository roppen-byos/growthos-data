"""Opt-in real Postgres checks; never use a shared/production database.

Requires three pre-created local test logins and explicit DDL authorization.
No database or login is created by these tests. See README.md.
"""

import os
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from growthos.contracts import validate_snapshot
from growthos.errors import GrowthOSError
from growthos.fixtures import SyntheticExtractor
from growthos.postgres import PostgresStore, assert_reader_permissions
from gwosdata.postgres import WriterStore, refresh
import growthos

ENABLED = os.environ.get("GROWTHOS_TEST_ALLOW_DDL") == "disposable-local-only"


@unittest.skipUnless(ENABLED, "Postgres integration not authorized/configured; requires an empty disposable local test database")
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import psycopg
            from psycopg import sql
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            raise AssertionError("Install the optional postgres dependency before authorizing integration tests.") from None
        cls.psycopg = psycopg
        connections = []
        try:
            for name in ("ADMIN", "WRITER", "READER"):
                dsn = os.environ.get(f"GROWTHOS_TEST_{name}_DSN", "")
                settings = conninfo_to_dict(dsn)
                if settings.get("host") not in ("localhost", "127.0.0.1", "::1"):
                    raise AssertionError("Integration tests accept explicitly local database endpoints only.")
                if not settings.get("dbname", "").endswith("_growthos_test"):
                    raise AssertionError("Integration test database name must end in _growthos_test.")
                connection = psycopg.connect(dsn, autocommit=True, connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=1000")
                connections.append(connection)
            cls.admin, cls.writer_connection, cls.reader_connection = connections
            identities = [c.execute("SELECT current_database(), current_user").fetchone() for c in connections]
            if len({identity[0] for identity in identities}) != 1 or len({identity[1] for identity in identities}) != 3:
                raise AssertionError("Use three different logins in one disposable database.")
            if cls.admin.execute("SELECT to_regnamespace('irf')").fetchone()[0] is not None:
                raise AssertionError("Test database must have no irf schema; existing data will not be deleted.")
            cls.admin.execute((Path(growthos.__file__).resolve().parents[1] / "sql/001_reporting.sql").read_text())
            # Only this explicitly disposable database is changed. No role creation.
            cls.admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            writer, reader = (sql.Identifier(identity[1]) for identity in identities[1:])
            cls.admin.execute(sql.SQL("GRANT USAGE ON SCHEMA irf TO {}, {}").format(writer, reader))
            cls.admin.execute(sql.SQL("GRANT SELECT ON irf.facts, irf.snapshots, irf.active_snapshot TO {}").format(reader))
            cls.admin.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON irf.refresh_runs, irf.active_snapshot TO {}").format(writer))
            cls.admin.execute(sql.SQL("GRANT SELECT, INSERT ON irf.facts, irf.snapshots TO {}").format(writer))
            cls.admin.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA irf TO {}").format(writer))
            cls.admin.execute(sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA irf TO {}").format(writer))
        except AssertionError:
            for connection in connections:
                connection.close()
            raise
        except Exception:
            for connection in connections:
                connection.close()
            raise AssertionError("Disposable Postgres setup failed; connection details withheld.") from None

    @classmethod
    def tearDownClass(cls):
        for connection in (cls.reader_connection, cls.writer_connection, cls.admin):
            connection.close()
        # Leave synthetic data for inspection. Dispose of the entire dedicated DB
        # yourself; tests never drop schemas/databases or delete unknown data.

    def setUp(self):
        self.writer = WriterStore(self.writer_connection, read_only=False, allow_nonproduction=True)
        self.reader = PostgresStore(self.reader_connection, read_only=True)
        self.baseline = self.publish()

    def publish(self, store=None, run_id=None):
        return refresh(store or self.writer, SyntheticExtractor(), run_id=run_id or "pg-" + uuid.uuid4().hex,
                       start="2026-09", end="2026-10")

    def test_database_reader_grants_are_enforced_without_readonly_session(self):
        assert_reader_permissions(self.reader_connection)
        self.assertEqual(self.reader.load(), self.baseline)
        for statement in (
            "UPDATE irf.facts SET installs=0",
            "DELETE FROM irf.active_snapshot",
            "INSERT INTO irf.active_snapshot SELECT * FROM irf.active_snapshot",
            "SELECT * FROM irf.refresh_runs",
            "CREATE TABLE irf.unapproved (id integer)",
        ):
            with self.subTest(statement=statement), self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                self.reader_connection.execute(statement)
        with self.assertRaises(GrowthOSError):
            assert_reader_permissions(self.writer_connection)

    def test_idempotency_and_pinned_reads(self):
        latest = self.publish()
        same = self.publish(run_id=latest.snapshot_id)
        self.assertEqual(same, latest)
        self.assertEqual(self.reader.load(self.baseline.snapshot_id), self.baseline)
        self.assertEqual(self.reader.load(), latest)
        count = self.admin.execute("SELECT count(*) FROM irf.facts WHERE snapshot_id=%s", (latest.snapshot_id,)).fetchone()[0]
        self.assertEqual(count, 4)

    def test_session_lock_and_crashed_run_recovery(self):
        with self.psycopg.connect(os.environ["GROWTHOS_TEST_WRITER_DSN"], autocommit=True, connect_timeout=5) as interrupted_connection:
            interrupted = WriterStore(interrupted_connection, read_only=False, allow_nonproduction=True)
            # Terminate only this test-owned backend, not the test's regular writer.
            # This exercises real session loss rather than voluntarily unlocking.
            with self.assertRaises(self.psycopg.OperationalError), interrupted.refresh_lock():
                old_id = "crashed-" + uuid.uuid4().hex
                old = interrupted.begin(old_id, ["2026-09", "2026-10"])
                interrupted.transition(old, "validating")
                with self.assertRaises(GrowthOSError) as caught, self.writer.refresh_lock():
                    self.fail("Concurrent writer must not acquire the lock.")
                self.assertEqual(caught.exception.code, "REFRESH_CONFLICT")
                pid = interrupted_connection.execute("SELECT pg_backend_pid()").fetchone()[0]
                self.assertEqual(self.admin.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone(), (True,))
                interrupted_connection.execute("SELECT 1")
        # The terminated session's lock is released; a fresh run recovers/fences it.
        latest = self.publish()
        with self.writer.refresh_lock():
            self.assertEqual(self.writer.get_run(old_id)["error_code"], "INTERRUPTED")
            candidate = validate_snapshot(SyntheticExtractor().extract(old_id, ["2026-09", "2026-10"]))
            with self.assertRaises(GrowthOSError) as caught:
                self.writer.publish(old, candidate)
            self.assertEqual(caught.exception.code, "STALE_REFRESH")
        self.assertEqual(self.reader.load(), latest)

    def test_real_transaction_rolls_back_partial_publication(self):
        underlying = self.writer_connection

        class FailPointerCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def __getattr__(self, name):
                return getattr(self.cursor, name)

            def execute(self, query, *args, **kwargs):
                if "INSERT INTO irf.active_snapshot" in query:
                    raise RuntimeError("synthetic publication fault after facts")
                return self.cursor.execute(query, *args, **kwargs)

        class FaultConnection:
            autocommit = True

            @contextmanager
            def cursor(self):
                with underlying.cursor() as cursor:
                    yield FailPointerCursor(cursor)

            def transaction(self):
                return underlying.transaction()

        run_id = "rollback-" + uuid.uuid4().hex
        faulty = WriterStore(FaultConnection(), read_only=False, allow_nonproduction=True)
        with self.assertRaises(GrowthOSError) as caught:
            self.publish(faulty, run_id)
        self.assertEqual(caught.exception.code, "PUBLICATION_OUTCOME_UNKNOWN")
        self.assertEqual(self.reader.load(), self.baseline)
        for table in ("snapshots", "facts"):
            # Test-only constants, not user-directed SQL.
            count = self.admin.execute(f"SELECT count(*) FROM irf.{table} WHERE snapshot_id=%s", (run_id,)).fetchone()[0]
            self.assertEqual(count, 0)

    def test_published_snapshots_reject_mutation_and_late_insert(self):
        snapshot_id = self.baseline.snapshot_id
        for statement in (
            "UPDATE irf.facts SET installs = 1 WHERE snapshot_id = %s",
            "DELETE FROM irf.snapshots WHERE snapshot_id = %s",
            "INSERT INTO irf.facts SELECT * FROM irf.facts WHERE snapshot_id = %s",
            "UPDATE irf.refresh_runs SET state='validating' WHERE run_id = %s",
        ):
            with self.subTest(statement=statement), self.assertRaises(self.psycopg.errors.RaiseException):
                self.admin.execute(statement, (snapshot_id,))
        self.assertEqual(self.reader.load(), self.baseline)


if __name__ == "__main__":
    unittest.main()
