"""Atomic publication, bounded verification reads, and protected local settings."""

from contextlib import contextmanager
import os
from pathlib import Path
import re

from .contracts import FACT_FIELDS, MAX_FACTS, MAX_MONTHS, checksum, month, validate_data
from .errors import reject
from .targets import METRICS, TARGET_DB_FIELDS, targets_from_records


def local_setting(key: str, path: Path | None, *, environ: dict | None = None) -> str:
    """Read one bounded literal setting, without sourcing files or mutating the environment."""
    env = os.environ if environ is None else environ
    value = env.get(key)
    if value:
        return value
    missing_msg = "Configure the required database DSN in protected local settings or the environment; never paste credentials in chat."
    if path is None:
        reject("CONFIGURATION", missing_msg)
    try:
        if path.is_symlink():
            reject("CONFIGURATION", "A regular bounded local settings file is required.")
        if not path.exists():
            reject("CONFIGURATION", missing_msg)
        if not path.is_file() or path.stat().st_size > 65536:
            reject("CONFIGURATION", "A regular bounded local settings file is required.")
        found = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, candidate = line.split("=", 1)
            name, candidate = name.strip(), candidate.strip()
            if candidate[:1] in ("'", '"'):
                if len(candidate) < 2 or candidate[-1] != candidate[0]:
                    reject("CONFIGURATION", "Malformed quoted local setting; details withheld.")
                candidate = candidate[1:-1]
            if name == key:
                found.append(candidate)
        if len(found) != 1 or not found[0]:
            reject("CONFIGURATION", missing_msg)
        return found[0]
    except (OSError, UnicodeError):
        reject("CONFIGURATION", "Local settings could not be read safely; details withheld.")


def validate_reader_settings(settings: dict) -> None:
    host = settings.get("host", "")
    if settings.get("sslmode") != "verify-full" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
        reject("TLS", "Reporting connections require a single explicit network hostname and sslmode=verify-full.")


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
    """Read back stored facts and targets for owner-operation verification."""

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

    def target_months(self, metric: str = "platform_ftu") -> list[str]:
        if metric not in METRICS:
            reject("ARGUMENTS", "Unsupported target metric.")
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('irf.target_imports')")
            if cursor.fetchone() == (None,):
                return []
            cursor.execute(
                "SELECT month FROM irf.target_imports WHERE metric=%s ORDER BY month LIMIT %s",
                (metric, MAX_MONTHS + 1),
            )
            return [row[0].isoformat()[:7] for row in cursor.fetchmany(MAX_MONTHS + 1)]

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


@contextmanager
def connect_writer():
    dsn = local_setting("GROWTHOS_WRITER_DSN", Path(__file__).resolve().parents[1] / ".env")
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        reject("DEPENDENCY", "Install the optional postgres dependency to publish monthly data.")
    try:
        settings = conninfo_to_dict(dsn)
    except Exception:
        reject("CONFIGURATION", "Invalid writer connection settings; details withheld.")
    validate_reader_settings(settings)
    try:
        connection = psycopg.connect(dsn, autocommit=True, connect_timeout=10,
            options="-c statement_timeout=60000 -c lock_timeout=5000")
    except Exception:
        reject("DATABASE_UNAVAILABLE", "Reporting writer connection failed; details withheld.")
    try:
        yield WriterStore(connection)
    finally:
        connection.close()


class WriterStore:
    def __init__(self, connection, *, allow_nonproduction=False):
        self.connection = connection
        self.allow_nonproduction = allow_nonproduction

    def replace_months(self, dataset):
        checked = validate_data(dataset.data)
        data = checked.data
        if data["nonproduction"] and not self.allow_nonproduction:
            reject("NONPRODUCTION_ONLY", "Synthetic data cannot be published to the live reporting store.")
        from psycopg.types.json import Jsonb
        dates = [month(m) for m in data["months"]]
        # Fetching and validation have already finished. No transaction spans OAuth.
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("DELETE FROM irf.facts WHERE month = ANY(%s)", (dates,))
            cursor.execute("DELETE FROM irf.month_metadata WHERE month = ANY(%s)", (dates,))
            metadata_rows = []
            for meta in data["month_metadata"]:
                month_facts = [r for r in data["facts"] if r["month"] == meta["month"]]
                metadata_rows.append((month(meta["month"]), Jsonb(meta), checksum(month_facts), len(month_facts)))
            cursor.executemany("INSERT INTO irf.month_metadata (month, metadata, result_checksum, fact_count) VALUES (%s,%s,%s,%s)", metadata_rows)
            fields = sorted(FACT_FIELDS)
            rows = []
            for fact in data["facts"]:
                row = dict(fact)
                row["month"] = month(row["month"])
                row["source_cities"] = Jsonb(row["source_cities"])
                rows.append(tuple(row[k] for k in fields))
            cursor.executemany(f"INSERT INTO irf.facts ({','.join(fields)}) VALUES ({','.join(['%s'] * len(fields))})", rows)
        return checked
