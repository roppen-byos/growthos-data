from datetime import date
import hashlib
from unittest.mock import patch

from gwosdata.warehouse_client import ALL_OPERATIONS, LANDED_OPERATIONS, MonthWindow, query_for
import unittest


class QueryContractTests(unittest.TestCase):
    def test_reviewed_sql_checksums_and_parameter_counts(self):
        # Pin reviewed SQL without requiring another repository or package.
        expected = {
            "installs-monthly": "433b93920294b9e7ddaf3b94dbaaf55d1039fbab10a53fa717db793812b2d7a2",
            "registrations-monthly": "7930c4aa3cc5086a930089049771d58a40dc7e84aceb0bb28ac53d6f33bf6ba5",
            "ftu-monthly": "58c3a0691f0e90f9a011f4e25ba4da6cc288f037b329ccf6c3d2c50e68b46e81",
            "funnel-monthly": "15dd535fd5e61ff83cb3be8f7c1536c280c94d006962f0d8ce29db89241159ec",
        }
        window = MonthWindow("2025-01")
        self.assertEqual(set(expected), {operation.value for operation in ALL_OPERATIONS})
        for operation in ALL_OPERATIONS:
            with self.subTest(operation=operation.value):
                spec = query_for(operation, window)
                self.assertEqual(expected[operation.value], spec.checksum)
                self.assertEqual(hashlib.sha256(spec.sql.encode()).hexdigest(), spec.checksum)
                self.assertEqual(spec.sql.count("?"), len(spec.parameters))

    def test_reviewed_month_to_date_landed_probe_checksums(self):
        # Operator-only freshness probes; not reporting queries, so they stay out of ALL_OPERATIONS.
        expected = {
            "landed-installs": "94e522564479827517451ecb012fe61701f81a852c7c666b014380099d37607d",
            "landed-registrations": "e5eb69b494b38ce7a01d8f3e81b2165e8bd1deb46ba67226167d67da5060dc6f",
            "landed-ftu": "a178cb3363df7d17339da532e8e4be0afd03105e621e2453c957384f2d3eb8bd",
            "landed-funnel": "df80360c38c5d53dc833aadd276d72c70e89ecce50beb451787b08493fc76d76",
            "landed-first-ride-lookup": "540e2b9863503598d47b6f7799e9288666bf05719d4b54cb7e3f2a5abcbc7976",
        }
        with patch("gwosdata.warehouse_client.business_today", return_value=date(2026, 10, 15)):
            window = MonthWindow.month_to_date("2026-10")
            self.assertEqual(set(expected), {operation.value for operation in LANDED_OPERATIONS})
            for operation in LANDED_OPERATIONS:
                with self.subTest(operation=operation.value):
                    spec = query_for(operation, window)
                    self.assertEqual(expected[operation.value], spec.checksum)
                    self.assertEqual(spec.sql.count("?"), len(spec.parameters))


if __name__ == "__main__":
    unittest.main()
