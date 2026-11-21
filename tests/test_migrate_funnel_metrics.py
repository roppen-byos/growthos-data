import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from growthos.errors import GrowthOSError
from gwosdata.migrate_funnel_metrics import main, migrate_connection


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, columns=("fe_sessions", "rr", "net", "bike_rr", "bike_net",
                                 "auto_rr", "auto_net", "cab_rr", "cab_net")):
        self.statements = []
        self.columns = columns

    def transaction(self):
        return FakeTransaction()

    def execute(self, statement):
        if statement.startswith("ALTER TABLE"):
            self.statements.append(statement)
            return FakeResult(())
        if statement.startswith("SELECT tablename"):
            return FakeResult((("facts",),))
        if statement.startswith("SELECT column_name"):
            return FakeResult(((column,) for column in self.columns))
        raise AssertionError(statement)


class FunnelMigrationTests(unittest.TestCase):
    def test_comment_header_is_not_executed_as_a_statement(self):
        connection = FakeConnection()
        result = migrate_connection(connection)
        self.assertTrue(result["migrated"])
        self.assertEqual(len(connection.statements), 9)
        self.assertTrue(all(s.startswith("ALTER TABLE irf.facts ADD COLUMN") for s in connection.statements))

    def test_missing_column_after_apply_rolls_back(self):
        with self.assertRaises(GrowthOSError) as caught:
            migrate_connection(FakeConnection(columns=("fe_sessions",)))
        self.assertEqual(caught.exception.code, "MIGRATION_STATE")

    def test_explicit_authorization_is_required_before_loading_owner_settings(self):
        with patch("gwosdata.migrate_funnel_metrics.run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 2)
            run.assert_not_called()
            run.return_value = {"migrated": True}
            self.assertEqual(main(["--authorize-migration"]), 0)
            run.assert_called_once_with(migrate_connection)


if __name__ == "__main__":
    unittest.main()
