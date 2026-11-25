"""Explicit owner-only removal of legacy version provenance from stored month metadata."""

from .contracts import FACT_FIELDS, MAX_FACTS, checksum, validate_data
from .errors import SafeParser, execute_cli, reject
from .postgres import PostgresStore

from .migrate_monthly import run

REMOVED_FIELDS = ("definition_version", "mapping_version", "reference_version")
REMOVED_READ_FIELDS = ("query_version",)


def _strip_versions(metadata: dict) -> tuple[dict, bool]:
    if not isinstance(metadata, dict):
        reject("STORE_INTEGRITY", "Stored month metadata, count or checksum is invalid; nothing changed.")
    stripped = {k: v for k, v in metadata.items() if k not in REMOVED_FIELDS}
    changed = set(stripped) != set(metadata)
    reads = stripped.get("source_reads")
    if isinstance(reads, list):
        clean_reads = []
        for read in reads:
            if not isinstance(read, dict):
                reject("STORE_INTEGRITY", "Stored month metadata, count or checksum is invalid; nothing changed.")
            clean_read = {k: v for k, v in read.items() if k not in REMOVED_READ_FIELDS}
            if set(clean_read) != set(read):
                changed = True
            clean_reads.append(clean_read)
        stripped["source_reads"] = clean_reads
    return stripped, changed


def migrate_connection(connection, writer_role=None, reader_role=None, *, allow_nonproduction=False):
    """Verify every month before stripping legacy version keys; any failure rolls back."""
    from psycopg.types.json import Jsonb

    changed_months = []
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
            if not isinstance(metadata, dict) or len(facts) != count or len(facts) > MAX_FACTS or checksum(facts) != digest or metadata.get("month") != m:
                reject("STORE_INTEGRITY", "Stored month metadata, count or checksum is invalid; nothing changed.")
            stripped, changed = _strip_versions(metadata)
            if not changed:
                continue
            expected = validate_data({"nonproduction": allow_nonproduction,
                "months": [m], "facts": facts, "month_metadata": [stripped]})
            connection.execute("UPDATE irf.month_metadata SET metadata=%s WHERE month=%s", (Jsonb(stripped), date))
            if PostgresStore(connection, allow_nonproduction=allow_nonproduction).load([m]) != expected:
                reject("STORE_INTEGRITY", "Stripped month metadata differs from expected; migration rolled back.")
            changed_months.append(m)
    return {"operation": "migrate-versions", "migrated": True,
            "changed_months": changed_months,
            "removed_fields": [*REMOVED_FIELDS, *(f"source_reads.{k}" for k in REMOVED_READ_FIELDS)]}


def main(argv=None):
    parser = SafeParser(description="Owner-only atomic migration: remove legacy version provenance from stored month metadata.")
    parser.add_argument("--authorize-migration", action="store_true", required=True)

    def execute():
        parser.parse_args(argv)
        return run(migrate_connection)

    return execute_cli(execute, failure_code="MIGRATION_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
