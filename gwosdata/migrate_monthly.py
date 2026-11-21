"""Explicit owner-only migration of the legacy reporting schema, preserving current data."""

from pathlib import Path

from growthos.contracts import CORE_FACT_FIELDS, FUNNEL_VOLUMES, checksum, reporting_schema_sql, validate_data
from growthos.errors import SafeParser, execute_cli, reject
from growthos.postgres import PostgresStore, validate_reader_settings

from .postgres import WriterStore, local_setting


def schema_sql():
    source = reporting_schema_sql()
    # The caller owns the transaction; SQL is a reviewed local schema, not user input.
    return source.replace("\nBEGIN;\n", "\n", 1).removesuffix("COMMIT;\n")


def grant_roles(connection, writer_role, reader_role):
    from psycopg import sql
    writer, reader = sql.Identifier(writer_role), sql.Identifier(reader_role)
    connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    connection.execute(sql.SQL("GRANT USAGE ON SCHEMA irf TO {}, {}").format(writer, reader))
    connection.execute(sql.SQL("GRANT SELECT ON irf.facts, irf.month_metadata TO {}").format(reader))
    connection.execute(sql.SQL("GRANT SELECT, INSERT, DELETE ON irf.facts, irf.month_metadata TO {}").format(writer))


def migrate_connection(connection, writer_role, reader_role):
    """One transaction: verify the old active rows, replace the schema, copy and recheck."""
    with connection.transaction():
        tables = {row[0] for row in connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='irf'").fetchall()}
        if tables != {"facts", "snapshots", "active_snapshot", "refresh_runs"}:
            reject("MIGRATION_STATE", "Expected only the legacy reporting tables; unknown or already migrated state was not changed.")
        active = connection.execute("SELECT s.metadata, s.result_checksum, s.fact_count, s.snapshot_id FROM irf.snapshots s JOIN irf.active_snapshot a USING (snapshot_id) WHERE a.singleton=true").fetchall()
        if len(active) != 1:
            reject("MIGRATION_STATE", "Expected exactly one active legacy dataset; nothing changed.")
        metadata, digest, count, old_id = active[0]
        fields = sorted(CORE_FACT_FIELDS)
        rows = connection.execute(f"SELECT {','.join(fields)} FROM irf.facts WHERE snapshot_id=%s ORDER BY month, city_key", (old_id,)).fetchmany(20_001)
        facts = [dict(zip(fields, row)) for row in rows]
        for row in facts:
            row["month"] = row["month"].isoformat()[:7]
            row.update({metric: None for metric in FUNNEL_VOLUMES})
        if len(facts) != count or checksum(facts) != digest or metadata.get("nonproduction") is not False:
            reject("STORE_INTEGRITY", "Legacy data count, checksum or production label is invalid; nothing changed.")
        dataset = validate_data({"nonproduction": False,
            "months": metadata["requested_months"], "facts": facts,
            "month_metadata": [{"month": m, "extracted_at": metadata["created_at"],
                "business_timezone": metadata["business_timezone"],
                "source_reads": [{key: value for key, value in read.items() if key not in ("cutoff", "query_version")}
                                 for read in metadata["source_reads"] if read["month"] == m]}
                for m in metadata["requested_months"]]})
        connection.execute("DROP TABLE irf.active_snapshot")
        connection.execute("DROP TABLE irf.facts")
        connection.execute("ALTER TABLE irf.refresh_runs DROP CONSTRAINT published_snapshot_fk")
        connection.execute("DROP TABLE irf.snapshots")
        connection.execute("DROP TABLE irf.refresh_runs")
        for name in ("reject_mutation", "check_candidate_insert", "check_run_transition"):
            # Intentionally no CASCADE: unknown dependencies must abort migration.
            connection.execute(f"DROP FUNCTION irf.{name}()")
        connection.execute(schema_sql().replace("CREATE SCHEMA irf;", "", 1))
        WriterStore(connection).replace_months(dataset)
        copied = PostgresStore(connection).load(dataset.data["months"])
        if copied != dataset:
            reject("STORE_INTEGRITY", "Copied monthly data differs from the legacy dataset; migration rolled back.")
        grant_roles(connection, writer_role, reader_role)
    return {"operation": "migrate-monthly", "migrated": True, "months": dataset.data["months"],
            "fact_count": len(facts), "result_checksum": dataset.result_checksum,
            "legacy_tables_removed": ["snapshots", "active_snapshot", "refresh_runs"]}


def run(migration=migrate_connection):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    root = Path(__file__).resolve().parents[1]
    try:
        settings = {name: conninfo_to_dict(local_setting(key, path)) for name, key, path in (
            ("owner", "GROWTHOS_OWNER_DSN", root / ".env.owner"),
            ("writer", "GROWTHOS_WRITER_DSN", root / ".env"),
            ("reader", "GROWTHOS_READER_DSN", root.parent / "gwos-analyst/.env"))}
    except Exception:
        reject("CONFIGURATION", "Migration requires protected local owner/writer/reader settings; details withheld.")
    for cfg in settings.values():
        validate_reader_settings(cfg)
        if any(cfg.get(key) != settings["owner"].get(key) for key in ("host", "port", "dbname")):
            reject("CONFIGURATION", "All migration settings must identify the same database endpoint.")
    if len({cfg.get("user") for cfg in settings.values()}) != 3:
        reject("CREDENTIAL_BOUNDARY", "Migration requires separate owner, writer and reader logins.")
    with psycopg.connect(**settings["owner"], autocommit=True, connect_timeout=10,
                         options="-c statement_timeout=60000 -c lock_timeout=5000") as conn:
        return migration(conn, settings["writer"]["user"], settings["reader"]["user"])


def main(argv=None):
    parser = SafeParser(description="Owner-only destructive legacy migration; preserves the active dataset, discards old revisions.")
    parser.add_argument("--authorize-migration", action="store_true", required=True)
    def execute():
        parser.parse_args(argv)
        return run()
    return execute_cli(execute, failure_code="MIGRATION_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
