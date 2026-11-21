"""Atomic current-month replacement. No revision, run, retry or locking machinery."""

from contextlib import contextmanager
from pathlib import Path

from growthos.contracts import FACT_FIELDS, checksum, month, validate_data
from growthos.errors import reject
from growthos.postgres import local_setting, validate_reader_settings


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
