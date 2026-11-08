"""Postgres publication adapter: writer connection, refresh lease/lock, and transactional snapshot activation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from growthos.contracts import FACT_FIELDS, Snapshot, month, month_range, reference, validate_snapshot
from growthos.errors import GrowthOSError, reject
from growthos.postgres import PostgresStore, validate_reader_settings

LOCK_NAMESPACE = 162878501
LOCK_KEY = 1


@dataclass(frozen=True)
class Lease:
    run_id: str
    generation: int


@contextmanager
def connect_writer():
    dsn = os.environ.get("GROWTHOS_WRITER_DSN")
    if not dsn:
        reject("CONFIGURATION", "Set GROWTHOS_WRITER_DSN outside chat before publishing a snapshot.")
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        reject("DEPENDENCY", "Install the optional postgres dependency to publish to the reporting database.")
    try:
        settings = conninfo_to_dict(dsn)
    except Exception:
        reject("CONFIGURATION", "Invalid writer connection settings; details withheld.")
    validate_reader_settings(settings)
    try:
        connection = psycopg.connect(
            dsn, autocommit=True, connect_timeout=10,
            options="-c statement_timeout=60000 -c lock_timeout=5000",
        )
    except Exception:
        reject("DATABASE_UNAVAILABLE", "Reporting database writer connection failed; connection details withheld.")
    try:
        yield WriterStore(connection, read_only=False)
    finally:
        connection.close()


class WriterStore(PostgresStore):
    """Publication-side store: refresh locking, run fencing and transactional activation."""

    @contextmanager
    def refresh_lock(self):
        self._writer(require_lock=False)
        if self._lock_held:
            reject("REFRESH_CONFLICT", "Refresh lock is already held by this process.")
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_NAMESPACE, LOCK_KEY))
            if cursor.fetchone() != (True,):
                reject("REFRESH_CONFLICT", "Another refresh is in progress; retry after it finishes.")
        self._lock_held = True
        try:
            yield
        finally:
            self._lock_held = False
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_NAMESPACE, LOCK_KEY))

    def get_run(self, run_id: str) -> dict | None:
        self._writer()
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT run_id, generation, state, requested_months, snapshot_id, error_code FROM irf.refresh_runs WHERE run_id = %s", (run_id,))
            row = cursor.fetchone()
        return None if row is None else dict(zip(("run_id", "generation", "state", "months", "snapshot_id", "error_code"), row))

    def recover_interrupted(self) -> None:
        self._writer()
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE irf.refresh_runs SET state = 'failed', error_code = 'INTERRUPTED', finished_at = clock_timestamp() WHERE state IN ('extracting','validating')")

    def begin(self, run_id: str, months: list[str]) -> Lease:
        self._writer()
        if not reference(run_id):
            reject("RUN_CONFLICT", "Run ID must be a bounded identifier.")
        from psycopg.types.json import Jsonb
        with self.connection.cursor() as cursor:
            cursor.execute("INSERT INTO irf.refresh_runs (run_id, state, requested_months) VALUES (%s, 'extracting', %s) RETURNING generation", (run_id, Jsonb(months)))
            return Lease(run_id, cursor.fetchone()[0])

    def transition(self, lease: Lease, state: str, error_code: str | None = None) -> None:
        self._writer()
        if state not in ("validating", "failed", "cancelled"):
            reject("RUN_STATE", "Refresh state transition is not permitted.")
        with self.connection.cursor() as cursor:
            cursor.execute("""
                UPDATE irf.refresh_runs SET state = %s, error_code = %s,
                    finished_at = CASE WHEN %s IN ('failed','cancelled') THEN clock_timestamp() ELSE NULL END
                WHERE run_id = %s AND generation = %s
                  AND generation = (SELECT max(generation) FROM irf.refresh_runs)
                  AND (state = 'extracting' OR (state = 'validating' AND %s IN ('failed','cancelled')))
            """, (state, error_code, state, lease.run_id, lease.generation, state))
            if cursor.rowcount != 1:
                reject("STALE_REFRESH", "An interrupted or older refresh cannot change the current run.")

    def publish(self, lease: Lease, snapshot: Snapshot) -> None:
        self._writer()
        snapshot = validate_snapshot(snapshot.data, publishable=True)
        data = snapshot.data
        if data["nonproduction"] and not self.allow_nonproduction:
            reject("NONPRODUCTION_ONLY", "Synthetic data is prohibited in the live reporting store.")
        if snapshot.snapshot_id != lease.run_id:
            reject("REQUEST_MISMATCH", "Snapshot must match the refresh run ID.")
        from psycopg.types.json import Jsonb
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("""
                SELECT requested_months FROM irf.refresh_runs
                WHERE run_id = %s AND generation = %s AND state = 'validating'
                  AND generation = (SELECT max(generation) FROM irf.refresh_runs) FOR UPDATE
            """, (lease.run_id, lease.generation))
            run = cursor.fetchone()
            if run is None:
                reject("STALE_REFRESH", "Only the newest validated refresh may publish.")
            if run[0] != data["requested_months"]:
                reject("REQUEST_MISMATCH", "Snapshot periods must match the requested refresh window.")
            metadata = {k: v for k, v in data.items() if k != "facts"}
            cursor.execute("INSERT INTO irf.snapshots (snapshot_id, generation, metadata, result_checksum, fact_count) VALUES (%s,%s,%s,%s,%s)", (lease.run_id, lease.generation, Jsonb(metadata), snapshot.result_checksum, len(data["facts"])))
            fields = sorted(FACT_FIELDS)
            columns = ",".join(["snapshot_id", *fields])
            placeholders = ",".join(["%s"] * (len(fields) + 1))
            values = []
            for fact in data["facts"]:
                row = dict(fact)
                row["month"] += "-01"
                row["source_cities"] = Jsonb(row["source_cities"])
                values.append((lease.run_id, *(row[k] for k in fields)))
            cursor.executemany(f"INSERT INTO irf.facts ({columns}) VALUES ({placeholders})", values)
            cursor.execute("""
                INSERT INTO irf.active_snapshot (singleton, snapshot_id, generation) VALUES (true,%s,%s)
                ON CONFLICT (singleton) DO UPDATE SET snapshot_id = EXCLUDED.snapshot_id, generation = EXCLUDED.generation
                WHERE irf.active_snapshot.generation < EXCLUDED.generation RETURNING snapshot_id
            """, (lease.run_id, lease.generation))
            if cursor.fetchone() is None:
                reject("STALE_REFRESH", "An older refresh cannot replace the active publication.")
            cursor.execute("UPDATE irf.refresh_runs SET state = 'published', snapshot_id = %s, finished_at = clock_timestamp() WHERE run_id = %s", (lease.run_id, lease.run_id))


def refresh(store: WriterStore, extractor, *, run_id: str, start: str, end: str) -> Snapshot:
    months = month_range(start, end)
    try:
        zone = ZoneInfo(extractor.business_timezone)
    except (AttributeError, ZoneInfoNotFoundError, ValueError):
        reject("TIMEZONE", "Refresh requires an explicitly approved source timezone before extraction.")
    if month(end) >= datetime.now(timezone.utc).astimezone(zone).date().replace(day=1):
        reject("INCOMPLETE_MONTH", "Only ended calendar months may be requested; no extraction was attempted.")
    with store.refresh_lock():
        existing = store.get_run(run_id)
        if existing is not None:
            if existing["state"] == "published" and existing["months"] == months:
                return store.load(existing["snapshot_id"])
            reject("RUN_CONFLICT", "Run ID was already attempted with a different window or did not publish; use a new run ID.")
        store.recover_interrupted()
        lease = store.begin(run_id, months)
        phase = "extracting"
        try:
            candidate = extractor.extract(run_id, months)
            store.transition(lease, "validating")
            snapshot = validate_snapshot(candidate, publishable=True)
            if snapshot.snapshot_id != run_id or snapshot.data["requested_months"] != months:
                reject("REQUEST_MISMATCH", "Candidate does not match the requested run and monthly window.")
            phase = "publishing"
            store.publish(lease, snapshot)
            return snapshot
        except KeyboardInterrupt:
            if phase == "publishing":
                reject("PUBLICATION_OUTCOME_UNKNOWN", "Interrupted during publication; inspect this run ID before retrying.")
            store.transition(lease, "cancelled", "CANCELLED")
            reject("CANCELLED", "Refresh cancelled; the previous verified snapshot remains active.")
        except GrowthOSError as error:
            store.transition(lease, "failed", error.code)
            raise
        except Exception:
            if phase == "publishing":
                reject("PUBLICATION_OUTCOME_UNKNOWN", "Publication acknowledgement failed; inspect this run ID before retrying.")
            store.transition(lease, "failed", "REFRESH_FAILED")
            reject("REFRESH_FAILED", "Refresh failed; source errors withheld and previous verified snapshot preserved.")
