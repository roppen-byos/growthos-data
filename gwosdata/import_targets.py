"""Owner import of the approved Platform FTU and registration targets; never extracts warehouse data."""

import csv
import hashlib
import io
from itertools import islice
from pathlib import Path
import re

from .classification import resolve_city
from .contracts import MAX_FACTS, MAX_MONTHS, month, text
from .errors import SafeParser, execute_cli, reject
from .postgres import PostgresStore
from .targets import METRICS, target_city_review, target_schema_sql, validate_targets

from .migrate_monthly import run

MAX_CSV_BYTES = 4 * 1024 * 1024
METRIC_COLUMNS = {"platform_ftu": "platform_ftu_target", "registrations": "registrations_target"}


def _count(value: str):
    if value == "":
        return None
    if not re.fullmatch(r"[0-9]{1,19}", value) or int(value) > 2**63 - 1:
        reject("TARGETS", "CSV targets must be nonnegative 64-bit integers or blank.")
    return int(value)


def parse_csv(source: bytes, metric: str = "platform_ftu"):
    """Validate the whole source before owner settings; apply only approved corrections."""
    if metric not in METRICS:
        reject("ARGUMENTS", "Unsupported target metric.")
    if not source or len(source) > MAX_CSV_BYTES:
        reject("BOUNDS", "Target CSV is empty or exceeds the bounded input size.")
    try:
        records = list(islice(csv.reader(io.StringIO(source.decode("utf-8-sig"), newline=""), strict=True), MAX_FACTS + 2))
    except (UnicodeError, csv.Error):
        reject("TARGETS", "Target CSV could not be parsed safely; source values withheld.")
    header = records[0] if records else []
    if header[:3] != ["city", "Tag", "LF-Tag"] or not 1 <= len(header) - 3 <= MAX_MONTHS:
        reject("TARGETS", "Expected city, Tag, LF-Tag and one to twelve monthly target columns.")
    months = []
    for label in header[3:]:
        match = re.fullmatch(r"([0-9]{4})-([0-9]{1,2})", label)
        if not match:
            reject("MONTH", "Target columns must identify year and calendar month.")
        value = f"{match[1]}-{int(match[2]):02d}"
        month(value)
        months.append(value)
    if months != sorted(set(months)):
        reject("PERIOD", "Target month columns must be ordered and unique after normalization.")
    if not 1 <= len(records) - 1 <= MAX_FACTS:
        reject("BOUNDS", "Target CSV contains an invalid number of source rows.")
    grouped, ludhiana_tags = {}, set()
    for values in records[1:]:
        if len(values) != len(header):
            reject("TARGETS", "Target CSV rows must match the declared column count.")
        city, tag, _ = values[:3]
        if not text(city):
            reject("CITY", "Target source cities must be nonempty bounded labels.")
        counts = [_count(value) for value in values[3:]]
        if city.casefold() == "ludhiana":
            if tag not in ("T2", "T3") or tag in ludhiana_tags:
                reject("TARGETS", "Ludhiana requires a unique T2 row; only its T3 duplicate may be discarded.")
            ludhiana_tags.add(tag)
            if tag == "T3":
                continue
        if city == "Grand Total":
            kind, key, display = "overall", "", city
        elif city == "New launch":
            kind, key, display = "new_launch", "", city
        else:
            if tag not in ("T1", "T2", "T3"):
                reject("TARGETS", "City target rows require a recognized source tag; actual tiers are unchanged.")
            kind = "city"
            key, display, _, _ = resolve_city(city, city)
        grain = kind, key
        if grain in grouped:
            source_names = {r["city"].casefold() for r in grouped[grain]["parts"]} | {city.casefold()}
            if kind != "city" or key != "trivandrum" or source_names != {"attingal", "trivandrum"} or len(grouped[grain]["parts"]) != 1:
                reject("DUPLICATE_GRAIN", "Unexpected duplicate target city; no unapproved summation was performed.")
            grouped[grain]["parts"].append({"city": city, "counts": counts})
        else:
            grouped[grain] = {"kind": kind, "city_key": key, "city": display,
                              "parts": [{"city": city, "counts": counts}]}
    if ludhiana_tags and "T2" not in ludhiana_tags:
        reject("TARGETS", "The retained T2 Ludhiana source row is unavailable.")
    if len(grouped) * len(months) > MAX_FACTS:
        reject("BOUNDS", "Target import exceeds the aggregate record limit.")
    column = METRIC_COLUMNS[metric]
    result = []
    for idx, value in enumerate(months):
        rows = []
        for group in grouped.values():
            parts = [p["counts"][idx] for p in group["parts"]]
            combined = None if any(part is None for part in parts) else sum(parts)
            row = {"month": value, **{k: group[k] for k in ("kind", "city_key", "city")},
                   "platform_ftu_target": None, "registrations_target": None}
            row[column] = combined
            rows.append(row)
        result.append(validate_targets({"month": value, "rows": rows}))
    return result


def load_csv(path: Path, metric: str = "platform_ftu"):
    try:
        with path.open("rb") as source:
            contents = source.read(MAX_CSV_BYTES + 1)
    except OSError:
        reject("TARGETS", "Target CSV could not be read; path and details withheld.")
    return parse_csv(contents, metric), hashlib.sha256(contents).hexdigest()


def _grant_reader(connection, writer_role, reader_role):
    from psycopg import sql
    writer, reader = sql.Identifier(writer_role), sql.Identifier(reader_role)
    for table in (sql.Identifier("irf", "targets"), sql.Identifier("irf", "target_imports")):
        connection.execute(sql.SQL("REVOKE ALL ON {} FROM {}, {}").format(table, writer, reader))
        connection.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(table, reader))
    connection.execute(sql.SQL("GRANT USAGE ON SCHEMA irf TO {}").format(reader))
    result = connection.execute("""SELECT bool_and(has_table_privilege(%s, name, 'SELECT')
        AND NOT has_table_privilege(%s, name, 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES'))
        FROM (VALUES ('irf.targets'), ('irf.target_imports')) AS t(name)
        """, (reader_role,) * 2).fetchone()
    if result != (True,):
        reject("READ_ONLY_ROLE", "Target reader must have SELECT-only access and no schema creation privileges.")


def _merge_review(connection, targets):
    store = PostgresStore(connection)
    available = store.available_months()
    by_month = {p.data["month"]: p for p in targets}
    review = []
    for value in ("2026-09", "2026-10"):
        if value not in by_month or value not in available:
            review.append({"month": value, "city_key": "trivandrum", "status": "target_or_actual_unavailable"})
        else:
            review.append(target_city_review(store.load([value]), by_month[value], "trivandrum"))
    return review


def _stored_months(connection, metric):
    rows = connection.execute(
        "SELECT month FROM irf.target_imports WHERE metric=%s ORDER BY month LIMIT %s",
        (metric, MAX_MONTHS + 1)).fetchmany(MAX_MONTHS + 1)
    return [row[0].isoformat()[:7] for row in rows]


def _stored_values(connection, value, metric):
    column = METRIC_COLUMNS[metric]
    rows = connection.execute(
        f"SELECT kind, city_key, {column} FROM irf.targets WHERE month=%s ORDER BY kind, city_key",
        (month(value),)).fetchall()
    return {(kind, key): target for kind, key, target in rows}


def _plan_values(plan, metric):
    column = METRIC_COLUMNS[metric]
    return {(r["kind"], r["city_key"]): r[column] for r in plan.data["rows"]}


def import_connection(connection, writer_role, reader_role, targets, metric="platform_ftu"):
    """Create/import/verify/grant atomically; never overwrite a conflicting stored metric."""
    if metric not in METRICS:
        reject("ARGUMENTS", "Unsupported target metric.")
    if not 1 <= len(targets) <= MAX_MONTHS:
        reject("BOUNDS", "Import one to twelve target months.")
    targets = [validate_targets(p.data) for p in targets]
    expected = {p.data["month"]: p for p in targets}
    if len(expected) != len(targets) or sum(len(p.data["rows"]) for p in targets) > MAX_FACTS:
        reject("TARGETS", "Target import requires unique months and bounded total records.")
    column = METRIC_COLUMNS[metric]
    with connection.transaction():
        present = connection.execute("SELECT to_regclass('irf.targets')").fetchone()[0] is not None
        imported = False
        if present:
            stored_months = _stored_months(connection, metric)
            if stored_months:
                if stored_months != sorted(expected):
                    reject("MIGRATION_STATE", "Existing target months differ; no overwrite was performed.")
                for value, plan in expected.items():
                    if _stored_values(connection, value, metric) != _plan_values(plan, metric):
                        reject("MIGRATION_STATE", "Existing targets differ from this import; no overwrite was performed.")
            else:
                imported = True
        else:
            connection.execute(target_schema_sql())
            imported = True
        if imported:
            with connection.cursor() as cursor:
                for plan in targets:
                    value = plan.data["month"]
                    cursor.executemany(
                        f"INSERT INTO irf.targets (month, kind, city_key, city, {column}) "
                        f"VALUES (%s, %s, %s, %s, %s) "
                        f"ON CONFLICT (month, kind, city_key) DO UPDATE SET {column}=EXCLUDED.{column}",
                        [(month(value), r["kind"], r["city_key"], r["city"], r[column]) for r in plan.data["rows"]],
                    )
                    cursor.execute(
                        "INSERT INTO irf.target_imports (month, metric) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (month(value), metric),
                    )
            for value, plan in expected.items():
                if _stored_values(connection, value, metric) != _plan_values(plan, metric):
                    reject("STORE_INTEGRITY", "Imported targets differ from the validated CSV; import rolled back.")
        _grant_reader(connection, writer_role, reader_role)
        review = _merge_review(connection, targets) if metric == "platform_ftu" else []
    return {"operation": "import-targets", "metric": metric, "imported": imported,
            "months": sorted(expected),
            "record_count": sum(len(p.data["rows"]) for p in targets), "merged_city_review": review}


def main(argv=None):
    parser = SafeParser(description="Owner target import for one metric; leaves monthly actuals unchanged.")
    parser.add_argument("--csv", type=Path, required=True, help="approved target CSV")
    parser.add_argument("--metric", choices=METRICS, required=True, help="metric stored by this CSV")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check-only", action="store_true", help="validate offline; no credentials or database")
    action.add_argument("--authorize-import", action="store_true", help="explicitly authorize owner table creation/import")

    def execute():
        args = parser.parse_args(argv)
        plans, digest = load_csv(args.csv, args.metric)
        if args.check_only:
            return {"operation": "check-targets", "metric": args.metric, "source_checksum": digest,
                    "months": [{"month": p.data["month"], "record_count": len(p.data["rows"]),
                                "result_checksum": p.result_checksum} for p in plans]}
        return run(lambda conn, writer, reader: import_connection(conn, writer, reader, plans, args.metric))

    return execute_cli(execute, failure_code="TARGET_IMPORT_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
