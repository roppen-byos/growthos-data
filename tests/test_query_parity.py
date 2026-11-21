from growthos.queries import warehouse_queries
from gwosdata.warehouse_client import ALL_OPERATIONS, MonthWindow, query_for
import unittest


class PackagedQueryParityTests(unittest.TestCase):
    def test_packaged_analyst_sql_matches_executed_warehouse_queries(self):
        window = MonthWindow("2025-01")
        packaged = warehouse_queries()
        self.assertEqual(set(packaged), {operation.value for operation in ALL_OPERATIONS})
        for operation in ALL_OPERATIONS:
            with self.subTest(operation=operation.value):
                spec = query_for(operation, window)
                entry = packaged[operation.value]
                self.assertEqual(entry["sql"], spec.sql)
                self.assertEqual(entry["checksum"], spec.checksum)
                self.assertEqual(entry["sql"].count("?"), len(spec.parameters))


if __name__ == "__main__":
    unittest.main()
