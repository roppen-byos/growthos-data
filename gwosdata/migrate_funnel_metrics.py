"""Owner-only additive migration: add funnel metric columns to irf.facts."""

from .contracts import funnel_metrics_sql
from .errors import SafeParser, execute_cli, reject

from .migrate_monthly import run


def migrate_connection(connection, writer_role=None, reader_role=None, *, allow_nonproduction=False):
    """Apply only additive nullable columns; existing rows and digests stay intact."""
    statements = [
        line.strip() for line in funnel_metrics_sql().splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    if not statements or any(not statement.startswith("ALTER TABLE irf.facts ") for statement in statements):
        reject("MIGRATION_STATE", "Packaged funnel migration contains unexpected statements; nothing changed.")
    with connection.transaction():
        tables = {row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='irf'").fetchall()}
        if "facts" not in tables:
            reject("MIGRATION_STATE", "Expected irf.facts before applying funnel columns; nothing changed.")
        for statement in statements:
            connection.execute(statement)
        columns = {row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='irf' AND table_name='facts'").fetchall()}
        required = {
            "fe_sessions", "rr", "net", "bike_rr", "bike_net",
            "auto_rr", "auto_net", "cab_rr", "cab_net",
        }
        if not required <= columns:
            reject("MIGRATION_STATE", "Funnel columns are missing after migration; rolled back.")
    return {"operation": "migrate-funnel-metrics", "migrated": True,
            "columns": sorted(required), "existing_rows_unchanged": True}


def main(argv=None):
    parser = SafeParser(description="Owner-only additive migration: add nullable funnel columns to irf.facts.")
    parser.add_argument("--authorize-migration", action="store_true", required=True)

    def execute():
        parser.parse_args(argv)
        return run(migrate_connection)

    return execute_cli(execute, failure_code="MIGRATION_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
