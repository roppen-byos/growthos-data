"""Owner-only atomic migration: legacy PFTU targets to the unified two-metric table."""

from .contracts import MAX_FACTS, month
from .errors import SafeParser, execute_cli, reject
from .postgres import read_target_month
from .targets import target_schema_sql, validate_targets

from .import_targets import _grant_reader
from .migrate_monthly import run

OLD_TABLE = "irf.pftu_targets"
NEW_TABLE = "irf.targets"
NEW_TABLES = {"targets", "target_imports"}


def migrate_connection(connection, writer_role, reader_role):
    """Copy legacy PFTU targets into the unified table; any mismatch rolls back."""
    with connection.transaction():
        tables = {row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='irf'").fetchall()}
        if NEW_TABLES & tables:
            reject("MIGRATION_STATE", "A unified targets table already exists; nothing changed.")
        if "pftu_targets" not in tables:
            reject("MIGRATION_STATE", "Expected the legacy PFTU targets table; nothing changed.")
        connection.execute(f"LOCK TABLE {OLD_TABLE} IN ACCESS EXCLUSIVE MODE")
        rows = connection.execute(
            f"SELECT month, kind, city_key, city, target FROM {OLD_TABLE} ORDER BY month, kind, city_key").fetchall()
        if not rows:
            reject("MIGRATION_STATE", "Legacy target table is empty; nothing changed.")
        if len(rows) > MAX_FACTS:
            reject("BOUNDS", "Legacy target table exceeds the bounded record limit.")
        months, expected, before = [], {}, {}
        for value, kind, key, city, target in rows:
            m = value.isoformat()[:7]
            if not months or months[-1] != m:
                months.append(m)
            expected.setdefault(m, []).append({
                "month": m, "kind": kind, "city_key": key, "city": city,
                "platform_ftu_target": target, "registrations_target": None,
            })
            count, total = before.get(m, (0, 0))
            before[m] = (count + 1, total + (0 if target is None else target))
        connection.execute(target_schema_sql())
        with connection.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO {NEW_TABLE} (month, kind, city_key, city, platform_ftu_target) "
                "VALUES (%s, %s, %s, %s, %s)", rows)
            cursor.executemany(
                "INSERT INTO irf.target_imports (month, metric) VALUES (%s, 'platform_ftu')",
                [(month(m),) for m in months])
        after = {}
        for value, count, total in connection.execute(
                f"SELECT month, count(*), coalesce(sum(platform_ftu_target), 0) "
                f"FROM {NEW_TABLE} GROUP BY month").fetchall():
            after[value.isoformat()[:7]] = (count, total)
        if after != before:
            reject("STORE_INTEGRITY", "Migrated target rows differ from the legacy table; migration rolled back.")
        for m in months:
            if read_target_month(connection, m).data != validate_targets(
                    {"month": m, "rows": expected[m]}).data:
                reject("STORE_INTEGRITY", "Migrated targets differ from the legacy values; migration rolled back.")
        connection.execute(f"DROP TABLE {OLD_TABLE}")
        _grant_reader(connection, writer_role, reader_role)
    return {"operation": "migrate-targets", "migrated": True, "months": months,
            "record_count": len(rows), "metrics": ["platform_ftu"]}


def main(argv=None):
    parser = SafeParser(description="Owner-only atomic migration to the unified targets table.")
    parser.add_argument("--authorize-migration", action="store_true", required=True)

    def execute():
        parser.parse_args(argv)
        return run(migrate_connection)

    return execute_cli(execute, failure_code="MIGRATION_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
