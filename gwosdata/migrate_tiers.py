"""Explicit owner-only T4 -> T3 relabeling of current monthly reporting data."""

from growthos.contracts import FACT_FIELDS, MAX_FACTS, checksum, validate_data
from growthos.errors import SafeParser, execute_cli, reject
from growthos.postgres import PostgresStore

from .migrate_monthly import run


def migrate_connection(connection, writer_role=None, reader_role=None, *, allow_nonproduction=False):
    """Verify every month before relabeling; any failure rolls back the whole migration."""
    changed_months, changed_rows = [], 0
    with connection.transaction():
        tables = {row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='irf'").fetchall()}
        if tables != {"facts", "month_metadata"}:
            reject("MIGRATION_STATE", "Expected only current monthly reporting tables; nothing changed.")
        connection.execute("LOCK TABLE irf.month_metadata, irf.facts IN ACCESS EXCLUSIVE MODE")
        stored = connection.execute(
            "SELECT month, metadata, result_checksum, fact_count FROM irf.month_metadata ORDER BY month").fetchall()
        fields = sorted(FACT_FIELDS)
        for date, metadata, digest, count in stored:
            m = date.isoformat()[:7]
            rows = connection.execute(
                f"SELECT {','.join(fields)} FROM irf.facts WHERE month=%s ORDER BY city_key LIMIT %s",
                (date, MAX_FACTS + 1)).fetchall()
            facts = [dict(zip(fields, row)) for row in rows]
            for row in facts:
                row["month"] = row["month"].isoformat()[:7]
            if len(facts) != count or len(facts) > MAX_FACTS or checksum(facts) != digest or metadata.get("month") != m:
                reject("STORE_INTEGRITY", "Stored month metadata, count or checksum is invalid; nothing changed.")
            relabeled = sum(row["tier"] == "T4" for row in facts)
            for row in facts:
                if row["tier"] == "T4":
                    row["tier"] = "T3"
            expected = validate_data({"nonproduction": allow_nonproduction,
                "months": [m], "facts": facts, "month_metadata": [metadata]})
            if relabeled:
                connection.execute("UPDATE irf.facts SET tier='T3' WHERE month=%s AND tier='T4'", (date,))
                connection.execute(
                    "UPDATE irf.month_metadata SET result_checksum=%s WHERE month=%s",
                    (expected.result_checksum, date))
                changed_months.append(m)
                changed_rows += relabeled
            if PostgresStore(connection, allow_nonproduction=allow_nonproduction).load([m]) != expected:
                reject("STORE_INTEGRITY", "Relabeled month differs from expected facts; migration rolled back.")
        connection.execute("ALTER TABLE irf.facts DROP CONSTRAINT facts_tier_check")
        connection.execute("ALTER TABLE irf.facts ADD CONSTRAINT facts_tier_check CHECK (tier IN ('T1','T2','T3'))")
    return {"operation": "migrate-tiers", "migrated": True,
            "changed_months": changed_months, "relabeled_fact_count": changed_rows,
            "allowed_tiers": ["T1", "T2", "T3"]}


def main(argv=None):
    parser = SafeParser(description="Owner-only atomic migration: fold stored T4 cities into T3, preserving all counts.")
    parser.add_argument("--authorize-migration", action="store_true", required=True)

    def execute():
        parser.parse_args(argv)
        return run(migrate_connection)

    return execute_cli(execute, failure_code="MIGRATION_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
