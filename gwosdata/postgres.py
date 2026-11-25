"""Atomic publication and protected local database settings."""

from contextlib import contextmanager
import os
from pathlib import Path
import re

from .contracts import FACT_FIELDS, checksum, month, validate_data
from .errors import reject


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
