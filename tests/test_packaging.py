import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

from gwosdata.contracts import funnel_metrics_sql, reporting_schema_sql
from gwosdata.targets import target_schema_sql

ROOT = Path(__file__).resolve().parents[1]


class StandalonePackagingTests(unittest.TestCase):
    def test_metadata_has_only_external_driver_extras_and_packages_sql(self):
        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(config["project"]["dependencies"], [])
        self.assertEqual(config["project"]["optional-dependencies"], {
            "warehouse": ["trino==0.336.0"], "postgres": ["psycopg[binary]>=3.2,<4"],
        })
        self.assertIn("sql/*.sql", config["tool"]["setuptools"]["package-data"]["gwosdata"])
        self.assertIn("CREATE TABLE irf.facts", reporting_schema_sql())
        self.assertIn("CREATE TABLE irf.targets", target_schema_sql())
        self.assertIn("ADD COLUMN IF NOT EXISTS fe_sessions", funnel_metrics_sql())

    def test_clean_checkout_plan_and_all_cli_imports_need_no_other_checkout_or_site_packages(self):
        with tempfile.TemporaryDirectory(prefix="gwos-data-standalone-", dir="/tmp/opencode") as temp:
            checkout = Path(temp) / "gwos-data"
            shutil.copytree(ROOT / "gwosdata", checkout / "gwosdata", ignore=shutil.ignore_patterns("__pycache__"))
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run(
                [sys.executable, "-S", "-m", "gwosdata.warehouse_cli", "--month", "2026-09"],
                cwd=checkout, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            plan = json.loads(result.stdout)
            self.assertFalse(plan["network_attempted"])
            self.assertEqual(len(plan["queries"]), 4)
            code = """
import importlib
from pathlib import Path
from gwosdata.contracts import reporting_schema_sql, funnel_metrics_sql
from gwosdata.targets import target_schema_sql
for path in Path('gwosdata').glob('*.py'):
    importlib.import_module('gwosdata.' + path.stem)
assert reporting_schema_sql() and target_schema_sql() and funnel_metrics_sql()
"""
            result = subprocess.run([sys.executable, "-S", "-c", code], cwd=checkout,
                                    env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
