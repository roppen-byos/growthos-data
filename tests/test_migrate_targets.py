import io
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import patch

from gwosdata.migrate_targets import main, migrate_connection


class Result:
    def __init__(self, rows=(), row=None):
        self._rows, self._row = list(rows), row

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._row


class Cursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, statement, rows):
        self.connection.writes.append((statement, list(rows)))


class Connection:
    """psycopg surface: executemany exists only on cursors, never on the connection."""

    def __init__(self, legacy):
        self.legacy = legacy
        self.writes = []
        self.statements = []
        self.dropped = []

    def __getattr__(self, name):
        if name == "executemany":
            raise AttributeError("'Connection' object has no attribute 'executemany'")
        raise AttributeError(name)

    def transaction(self):
        connection = self

        class Transaction:
            def __enter__(self):
                return connection

            def __exit__(self, *exc):
                return False

        return Transaction()

    def cursor(self):
        return Cursor(self)

    def execute(self, statement, params=None):
        self.statements.append(statement)
        if statement.startswith("SELECT tablename"):
            return Result(rows=[("pftu_targets",)])
        if statement.startswith("SELECT month, kind"):
            return Result(rows=self.legacy)
        if statement.startswith("SELECT month, count(*)"):
            totals = {}
            for month, _kind, _key, _city, target in self.legacy:
                count, total = totals.get(month, (0, 0))
                totals[month] = (count + 1, total + (0 if target is None else target))
            return Result(rows=[(m, c, t) for m, (c, t) in totals.items()])
        if statement.startswith("DROP TABLE"):
            self.dropped.append(statement)
        return Result()


class MigrateTargetsTests(unittest.TestCase):
    def setUp(self):
        self.legacy = [
            (date(2026, 9, 1), "city", "harbor", "Harbor", 80),
            (date(2026, 9, 1), "new_launch", "", "New launch", 60),
            (date(2026, 9, 1), "overall", "", "Grand Total", 150),
        ]

    def test_writes_use_a_cursor_because_connections_have_no_executemany(self):
        connection = Connection(self.legacy)
        with patch("gwosdata.migrate_targets.read_target_month", return_value=type("T", (), {"data": {}})()), \
                patch("gwosdata.migrate_targets.validate_targets", return_value=type("T", (), {"data": {}})()), \
                patch("gwosdata.migrate_targets._grant_reader"):
            result = migrate_connection(connection, "writer", "reader")
        self.assertEqual(result["months"], ["2026-09"])
        self.assertEqual(result["record_count"], 3)
        self.assertEqual(len(connection.writes), 2)
        self.assertIn("INSERT INTO irf.targets", connection.writes[0][0])
        self.assertIn("platform_ftu_target", connection.writes[0][0])
        self.assertEqual(connection.writes[0][1][0][-1], 80)
        self.assertIn("irf.target_imports", connection.writes[1][0])
        self.assertEqual(connection.writes[1][1], [(date(2026, 9, 1),)])
        self.assertEqual(len(connection.dropped), 1)

    def test_explicit_authorization_is_required_before_owner_settings(self):
        with patch("gwosdata.migrate_targets.run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 2)
            run.assert_not_called()
            run.return_value = {"migrated": True}
            self.assertEqual(main(["--authorize-migration"]), 0)
            run.assert_called_once_with(migrate_connection)


if __name__ == "__main__":
    unittest.main()
