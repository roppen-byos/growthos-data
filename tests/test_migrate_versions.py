import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from gwosdata.migrate_versions import main, migrate_connection


class VersionMigrationCliTests(unittest.TestCase):
    def test_explicit_authorization_is_required_before_loading_owner_settings(self):
        with patch("gwosdata.migrate_versions.run") as run, redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 2)
            run.assert_not_called()
            run.return_value = {"migrated": True}
            self.assertEqual(main(["--authorize-migration"]), 0)
            run.assert_called_once_with(migrate_connection)
