import hashlib
from gwosdata.warehouse_client import ALL_OPERATIONS, MonthWindow, query_for
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


if __name__ == "__main__":
    unittest.main()
