"""Test-only bounded reader for verifying published facts, targets, and permissions."""

from gwosdata.contracts import FACT_FIELDS, MAX_FACTS, MAX_MONTHS, checksum, exact_fields, month, validate_data
from gwosdata.errors import reject
from gwosdata.targets import TARGET_DB_FIELDS, TargetMonth, validate_targets


def targets_from_records(records: list[dict]) -> TargetMonth:
    """Validate one bounded SQL result; stored rows carry no separate provenance."""
    if not records:
        reject("TARGETS_UNAVAILABLE", "Targets are unavailable for the requested month; no substitute was selected.")
    if len(records) > MAX_FACTS:
        reject("BOUNDS", "Too many target records for one month.")
    first = records[0]
    for row in records:
        exact_fields(row, set(TARGET_DB_FIELDS))
        if row["month"] != first["month"]:
            reject("STORE_INTEGRITY", "Stored target records disagree on their month.")
    return validate_targets({"month": first["month"], "rows": records})


def assert_reader_permissions(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT bool_and(has_table_privilege(current_user, name, 'SELECT')
                AND NOT has_table_privilege(current_user, name, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES'))
            FROM (VALUES ('irf.facts'), ('irf.month_metadata')) AS reporting(name)
        """)
        if cursor.fetchone() != (True,):
            reject("READ_ONLY_ROLE", "Analysis requires a dedicated read-only login for monthly reporting tables.")


def read_target_month(connection, value: str):
    """Bounded verification read; the caller owns connection permissions."""
    selected = month(value)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {','.join(TARGET_DB_FIELDS)} FROM irf.targets WHERE month=%s ORDER BY kind, city_key LIMIT %s",
            (selected, MAX_FACTS + 1),
        )
        records = [dict(zip(TARGET_DB_FIELDS, row)) for row in cursor.fetchmany(MAX_FACTS + 1)]
    for row in records:
        row["month"] = row["month"].isoformat()[:7]
    result = targets_from_records(records)
    if result.data["month"] != value:
        reject("STORE_INTEGRITY", "Stored target month does not match the request.")
    return result


class PostgresStore:
    """Read back stored facts and targets for test verification."""

    def __init__(self, connection, *, allow_nonproduction=False):
        self.connection = connection
        self.allow_nonproduction = allow_nonproduction

    def available_months(self) -> list[str]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT month FROM irf.month_metadata ORDER BY month")
            return [row[0].isoformat()[:7] for row in cursor.fetchall()]

    def load_targets(self, value: str):
        month(value)
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('irf.targets'), to_regclass('irf.target_imports')")
            if cursor.fetchone() != ('irf.targets', 'irf.target_imports'):
                reject("TARGETS_UNAVAILABLE", "Stored targets are not installed; existing monthly analysis remains available.")
            cursor.execute("""SELECT has_table_privilege(current_user, 'irf.targets', 'SELECT')
                AND NOT has_table_privilege(current_user, 'irf.targets',
                    'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
                AND has_table_privilege(current_user, 'irf.target_imports', 'SELECT')
                AND NOT has_table_privilege(current_user, 'irf.target_imports',
                    'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')
                AND NOT has_schema_privilege(current_user, 'irf', 'CREATE')""")
            if cursor.fetchone() != (True,):
                reject("READ_ONLY_ROLE", "Target analysis requires SELECT-only access to stored targets.")
        return read_target_month(self.connection, value)

    def load(self, months: list[str] | None = None):
        selected = self.available_months()[-2:] if months is None else months
        if not selected or len(selected) > MAX_MONTHS:
            reject("COVERAGE", "Select one to twelve available stored months.")
        dates = [month(m) for m in selected]
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT month, metadata, result_checksum, fact_count FROM irf.month_metadata WHERE month = ANY(%s) ORDER BY month", (dates,))
            stored = cursor.fetchall()
            if [row[0].isoformat()[:7] for row in stored] != sorted(set(selected)) or len(selected) != len(stored):
                reject("COVERAGE", "Requested months are unavailable; no substitute was selected.")
            fields = sorted(FACT_FIELDS)
            cursor.execute(f"SELECT {','.join(fields)} FROM irf.facts WHERE month = ANY(%s) ORDER BY month, city_key LIMIT %s", (dates, MAX_FACTS + 1))
            rows = cursor.fetchmany(MAX_FACTS + 1)
        if len(rows) > MAX_FACTS:
            reject("BOUNDS", "Too many facts for one read; select fewer months.")
        facts = [dict(zip(fields, row)) for row in rows]
        for fact in facts:
            fact["month"] = fact["month"].isoformat()[:7]
        result = validate_data({"nonproduction": self.allow_nonproduction,
            "months": sorted(selected), "month_metadata": [r[1] for r in stored], "facts": facts})
        facts_by_month: dict[str, list[dict]] = {m: [] for m in selected}
        for row in result.data["facts"]:
            facts_by_month[row["month"]].append(row)
        for value, metadata, digest, count in stored:
            m = value.isoformat()[:7]
            current = facts_by_month.get(m, [])
            if metadata["month"] != m or len(current) != count or checksum(current) != digest:
                reject("STORE_INTEGRITY", "Stored month metadata, count or checksum is invalid.")
        return result
