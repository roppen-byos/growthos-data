"""Explicit owner-only merge of the two unattributed-city sentinel buckets.

Warehouse extractions previously produced `unrecognized_city` for registrations and
`city_not_found` for installs. Both now canonicalise to `city_not_found`; this
one-time migration collapses already-published months without re-extraction.
"""

from growthos.classification import UNATTRIBUTED_CITY_KEY, UNATTRIBUTED_SOURCE_KEYS
from growthos.contracts import FACT_FIELDS, MAX_FACTS, checksum, month, validate_data
from growthos.errors import SafeParser, execute_cli, reject
from growthos.postgres import PostgresStore

from .migrate_monthly import run

TARGET_KEY = UNATTRIBUTED_CITY_KEY
SOURCE_KEYS = tuple(sorted(UNATTRIBUTED_SOURCE_KEYS - {UNATTRIBUTED_CITY_KEY}))
SOURCE_KEY = SOURCE_KEYS[0]
_BASE_TABLES = {"facts", "month_metadata"}
_TARGET_TABLES = {"targets", "target_imports"}
_METRICS = tuple(sorted(FACT_FIELDS - {"month", "city_key", "city", "tier", "source_cities"}))


def check_tables(names: set[str]) -> None:
    """Allow the reporting tables plus an installed target pair, and nothing else."""
    if names - _TARGET_TABLES != _BASE_TABLES or (_TARGET_TABLES & names and not _TARGET_TABLES <= names):
        reject("MIGRATION_STATE", "Expected only current monthly reporting tables; nothing changed.")


def _merge_metric(current, incoming):
    values = [value for value in (current, incoming) if value is not None]
    return sum(values) if values else None


def merge_facts(facts: list[dict]) -> tuple[list[dict], int]:
    """Collapse every `unrecognized_city` row into `city_not_found` within one month.

    Returns the rewritten fact list and the number of source rows merged. A month
    without the old sentinel is returned unchanged, so the migration is idempotent.
    """
    source = [row for row in facts if row["city_key"] == SOURCE_KEY]
    if not source:
        return facts, 0
    source_count = len(source)
    kept = [row for row in facts if row["city_key"] != SOURCE_KEY]
    target = next((row for row in kept if row["city_key"] == TARGET_KEY), None)
    if target is None:
        target = dict(source[0])
        kept.append(target)
        source = source[1:]
    elif target["tier"] != "T3":
        # irf.facts requires tier IN ('T1','T2','T3'); unexpected T1/T2 on the sentinel is corrupt state.
        reject("MIGRATION_STATE", "The city_not_found bucket is not T3; nothing changed.")
    # DB column stays 'T3' to satisfy the table CHECK constraint; analyst scopes exclude the sentinel from all tiers.
    target.update(city_key=TARGET_KEY, city=TARGET_KEY, tier="T3", source_cities=[TARGET_KEY])
    for row in source:
        for metric in _METRICS:
            target[metric] = _merge_metric(target[metric], row[metric])
    return kept, source_count


def migrate_connection(connection, writer_role=None, reader_role=None, *, allow_nonproduction=False):
    """Verify every month before merging sentinels; any failure rolls back."""
    from psycopg.types.json import Jsonb

    changed_months, merged_rows = [], 0
    with connection.transaction():
        tables = {row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='irf'").fetchall()}
        check_tables(tables)
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
            if (not isinstance(metadata, dict) or len(facts) != count or len(facts) > MAX_FACTS
                    or checksum(facts) != digest or metadata.get("month") != m):
                reject("STORE_INTEGRITY", "Stored month metadata, count or checksum is invalid; nothing changed.")
            merged, folded = merge_facts(facts)
            if not folded:
                continue
            expected = validate_data({"nonproduction": allow_nonproduction,
                "months": [m], "facts": merged, "month_metadata": [metadata]})
            target = next(row for row in merged if row["city_key"] == TARGET_KEY)
            connection.execute("DELETE FROM irf.facts WHERE month=%s AND city_key=%s", (date, SOURCE_KEY))
            insert_fields = sorted(FACT_FIELDS)
            values = dict(target)
            values["month"] = month(values["month"])
            values["source_cities"] = Jsonb(values["source_cities"])
            connection.execute(
                f"INSERT INTO irf.facts ({','.join(insert_fields)}) VALUES ({','.join(['%s'] * len(insert_fields))})"
                f" ON CONFLICT (month, city_key) DO UPDATE SET "
                + ", ".join(f"{name}=EXCLUDED.{name}" for name in insert_fields if name not in ("month", "city_key")),
                tuple(values[name] for name in insert_fields))
            connection.execute(
                "UPDATE irf.month_metadata SET result_checksum=%s, fact_count=%s WHERE month=%s",
                (expected.result_checksum, len(merged), date))
            if PostgresStore(connection, allow_nonproduction=allow_nonproduction).load([m]) != expected:
                reject("STORE_INTEGRITY", "Merged month differs from expected facts; migration rolled back.")
            changed_months.append(m)
            merged_rows += folded
    return {"operation": "migrate-city-sentinel", "migrated": True,
            "changed_months": changed_months, "merged_fact_count": merged_rows,
            "sentinel": TARGET_KEY}


def main(argv=None):
    parser = SafeParser(description="Owner-only atomic merge: fold stored unrecognized_city rows into city_not_found, preserving all counts.")
    parser.add_argument("--authorize-migration", action="store_true", required=True)

    def execute():
        parser.parse_args(argv)
        return run(migrate_connection)

    return execute_cli(execute, failure_code="MIGRATION_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())