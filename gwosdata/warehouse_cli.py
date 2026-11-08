"""Two-month city-level Trino extractor, candidate staging, CSV reconciliation, and operator CLI."""

from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile

from growthos.analysis import number, quotient
from growthos.classification import MAPPING_VERSION, REFERENCE_VERSION, resolve_city
from growthos.contracts import (
    CERTIFICATIONS, MAX_FACTS, RATIOS, SCHEMA_VERSION, VOLUMES,
    Snapshot, month_range, reference, validate_snapshot,
)
from growthos.errors import MAX_OUTPUT_BYTES, SafeParser, execute_cli, reject
from .postgres import connect_writer, refresh as publish_refresh
from .warehouse_client import (
    BIKE_SERVICES, BUSINESS_TIMEZONE, EXTRACTION_OPERATIONS, OPERATOR_ENV_FILE,
    QUERY_VERSION, MonthWindow, Operation, WarehouseSession, operator_environment, query_for,
)

DEFINITION_VERSION = "irf-definitions-dec005-dec006-v1"
MAX_CANDIDATE_BYTES = 16 * 1024 * 1024
DEFAULT_REFERENCE_CSV = Path(__file__).resolve().parents[3] / "irf/data/raw/IRF_Updated - Dec-Oct.csv"
CSV_METRIC_MAP = {
    "installs": "installs",
    "registrations": "entity_reg",
    "platform_ftu": "Platform_FTU",
    "platform_link_ftu": "Platform_Link_FTU",
    "platform_auto_ftu": "Platform_Auto_FTU",
    "platform_cab_ftu": "Platform_Cab_FTU",
}


class WarehouseExtractor:
    """Extract monthly canonical-city IRF facts under one interactive OAuth session."""

    business_timezone = BUSINESS_TIMEZONE

    def __init__(self, session: WarehouseSession, *, certified: bool = False, certification_ref: str = "dec-005-dec006-verified-v1"):
        self.session = session
        self.certified = certified
        self.certification_ref = certification_ref
        self.coverage_status = "verified_complete" if certified else "unknown"
        self.coverage_evidence = certification_ref if certified else None

    def extract(self, run_id: str, months: list[str]) -> dict:
        if not reference(run_id):
            reject("PROVENANCE", "Snapshot run ID must be a bounded identifier.")
        if not isinstance(months, list) or not months:
            reject("PERIOD", "Specify at least one calendar month.")
        ordered_months = month_range(months[0], months[-1])
        if months != ordered_months:
            reject("PERIOD", "Requested months must be ordered, unique and contiguous.")
        windows = [MonthWindow(m) for m in ordered_months]
        self.session.authorize(windows[0], extra_windows=tuple(windows[1:]))

        source_reads = []
        month_city_metrics: dict[tuple[str, str], dict[str, int | None]] = {}
        city_samples: dict[str, set[str]] = defaultdict(set)

        def ensure_cell(m: str, city_key: str) -> dict[str, int | None]:
            cell = month_city_metrics.get((m, city_key))
            if cell is None:
                cell = {metric: None for metric in VOLUMES}
                month_city_metrics[(m, city_key)] = cell
            return cell

        for window in windows:
            m = window.value
            installs_read = self.session.run(Operation.INSTALLS_MONTHLY, window)
            registrations_read = self.session.run(Operation.REGISTRATIONS_MONTHLY, window)
            ftu_read = self.session.run(Operation.FTU_MONTHLY, window)

            for source_name, read in (("installs", installs_read), ("registrations", registrations_read), ("ftu", ftu_read)):
                source_reads.append({
                    "source": source_name, "month": m, "query_version": QUERY_VERSION,
                    "started_at": read["started_at"], "completed_at": read["completed_at"],
                    "cutoff": read["completed_at"] if self.certified else None,
                })

            for row in installs_read["rows"]:
                key = row["canonical_city_key"]
                city_samples[key].add(row["sample_source_city"])
                ensure_cell(m, key)["installs"] = row["installs"]

            for row in registrations_read["rows"]:
                key = row["canonical_city_key"]
                city_samples[key].add(row["sample_source_city"])
                ensure_cell(m, key)["registrations"] = row["registrations"]

            for row in ftu_read["rows"]:
                key = row["canonical_city_key"]
                city_samples[key].add(row["sample_source_city"])
                cell = ensure_cell(m, key)
                for col in ("platform_ftu", "platform_link_ftu", "platform_auto_ftu", "platform_cab_ftu"):
                    cell[col] = row[col]

        resolved_cities = {
            key: resolve_city(key, min(samples))[1:]
            for key, samples in city_samples.items()
        }
        facts = [
            {
                "month": m, "city_key": key, "city": resolved_cities[key][0],
                "tier": resolved_cities[key][1], "source_cities": list(resolved_cities[key][2]),
                **metrics,
            }
            for (m, key), metrics in sorted(month_city_metrics.items())
        ]
        cert_val = self.certification_ref if self.certified else None
        return {
            "schema_version": SCHEMA_VERSION, "snapshot_id": run_id,
            "nonproduction": self.session.nonproduction,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "business_timezone": BUSINESS_TIMEZONE, "definition_version": DEFINITION_VERSION,
            "mapping_version": MAPPING_VERSION, "reference_version": REFERENCE_VERSION,
            "certification": {name: cert_val for name in CERTIFICATIONS},
            "requested_months": ordered_months,
            "coverage": [{"month": m, "status": self.coverage_status, "evidence": self.coverage_evidence} for m in ordered_months],
            "source_reads": source_reads, "facts": facts,
        }


def known_subtotal_comparison(snapshot: Snapshot, previous: str, latest: str) -> dict:
    data = snapshot.data
    prev_rows = [r for r in data["facts"] if r["month"] == previous]
    curr_rows = [r for r in data["facts"] if r["month"] == latest]
    before = {m: sum(r[m] for r in prev_rows if r[m] is not None) for m in VOLUMES}
    after = {m: sum(r[m] for r in curr_rows if r[m] is not None) for m in VOLUMES}
    volumes, ratios = {}, {}
    with localcontext() as context:
        context.prec = 40
        for metric in VOLUMES:
            a, b = before[metric], after[metric]
            delta = b - a
            volumes[metric] = {
                "previous_known_subtotal": a, "latest_known_subtotal": b,
                "absolute_delta": delta, "growth_fraction": number(quotient(delta, a)),
            }
        for metric, (num_col, den_col) in RATIOS.items():
            a, b = quotient(before[num_col], before[den_col]), quotient(after[num_col], after[den_col])
            ratios[metric] = {
                "previous": number(a), "latest": number(b),
                "delta_pp": number(None if a is None or b is None else 100 * (b - a)),
            }
    return {"previous": previous, "latest": latest, "volumes": volumes, "ratios": ratios}


def reconcile_with_csv(snapshot: Snapshot, csv_path: Path) -> dict:
    content = csv_path.read_bytes()
    csv_sha256 = hashlib.sha256(content).hexdigest()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    csv_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    csv_cities: dict[str, set[str]] = defaultdict(set)
    for row in reader:
        raw_time = row["time_value"].strip()
        try:
            parsed = datetime.strptime(raw_time, "%Y-%m-%d" if len(raw_time) == 10 else "%d/%m/%y")
        except ValueError:
            continue
        m = f"{parsed.year:04d}-{parsed.month:02d}"
        csv_cities[m].add(row["city"].strip().casefold())
        for metric, csv_col in CSV_METRIC_MAP.items():
            val = row[csv_col].strip().replace(",", "")
            if val not in ("", "-", "nan"):
                csv_totals[m][metric] += int(Decimal(val))

    data = snapshot.data
    by_month = {}
    with localcontext() as context:
        context.prec = 40
        for m in data["requested_months"]:
            m_rows = [r for r in data["facts"] if r["month"] == m]
            metrics_comp = {}
            for metric in VOLUMES:
                wh_val = sum(r[metric] for r in m_rows if r[metric] is not None)
                ref_val = csv_totals[m].get(metric, 0)
                diff = wh_val - ref_val
                metrics_comp[metric] = {
                    "warehouse_total": wh_val, "csv_total": ref_val,
                    "absolute_diff": diff, "relative_diff_pct": number(quotient(100 * diff, ref_val)),
                }
            by_month[m] = {
                "warehouse_canonical_cities": len(m_rows),
                "csv_distinct_city_keys": len(csv_cities[m]),
                "metrics": metrics_comp,
            }
    return {"reference_csv_sha256": csv_sha256, "months": by_month}


def stage_candidate_file(path: Path, snapshot: Snapshot) -> Path:
    destination = Path(path).resolve()
    if destination.exists() and destination.is_symlink():
        reject("CANDIDATE_FILE", "Candidate output path must not be a symlink.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(snapshot.payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, destination)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return destination


def load_candidate_file(path: Path, *, publishable: bool = False) -> Snapshot:
    target = Path(path)
    try:
        if target.is_symlink():
            reject("CANDIDATE_FILE", "Candidate file must not be a symlink.")
        raw = target.read_bytes()
        if len(raw) > MAX_CANDIDATE_BYTES:
            reject("CANDIDATE_FILE", "Candidate file exceeds the bounded size limit.")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        reject("CANDIDATE_FILE", "Candidate snapshot file could not be read safely.")
    if isinstance(payload, dict) and isinstance(payload.get("facts"), list) and len(payload["facts"]) > MAX_FACTS:
        reject("BOUNDS", "Candidate fact count exceeds the maximum bound.")
    return validate_snapshot(payload, publishable=publishable)


class _StagedCandidateExtractor:
    business_timezone = BUSINESS_TIMEZONE

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot

    def extract(self, run_id: str, months: list[str]) -> dict:
        return self.snapshot.data


def run(args):
    if args.publish_candidate:
        snapshot = load_candidate_file(Path(args.publish_candidate), publishable=True)
        req_months = snapshot.data["requested_months"]
        with connect_writer() as store:
            published = publish_refresh(
                store,
                _StagedCandidateExtractor(snapshot),
                run_id=snapshot.snapshot_id,
                start=req_months[0],
                end=req_months[-1],
            )
        return {
            "operation": "warehouse-publish-candidate",
            "snapshot_id": published.snapshot_id,
            "result_checksum": published.result_checksum,
            "covered_months": published.data["requested_months"],
            "fact_count": len(published.data["facts"]),
            "published": True,
        }
    if not args.month:
        reject("ARGUMENTS", "Supply --month YYYY-MM (or --publish-candidate PATH).")
    end_month = args.end_month or args.month
    months = month_range(args.month, end_month)
    windows = [MonthWindow(m) for m in months]
    run_id = args.run_id or f"candidate-{months[0]}-{months[-1]}"
    candidate_path = Path(args.extract_candidate or f"data/candidates/{run_id}.json")
    if not args.authorize_live:
        return {
            "operation": "warehouse-extraction-plan", "network_attempted": False,
            "publication_available": False, "requested_months": months,
            "run_id": run_id, "bike_services": list(BIKE_SERVICES),
            "queries": [
                {"month": w.value, "operation": op.value, "query_checksum": query_for(op, w).checksum}
                for w in windows for op in EXTRACTION_OPERATIONS
            ],
        }
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        reject("OPERATOR_TERMINAL", "Live extraction requires the human operator's terminal.")
    config = operator_environment(env_file=OPERATOR_ENV_FILE)
    with WarehouseSession(config, live_authorized=True, interactive=True) as session:
        extractor = WarehouseExtractor(session, certified=True)
        payload = extractor.extract(run_id, months)
    snapshot = validate_snapshot(payload, publishable=True)
    staged_path = stage_candidate_file(candidate_path, snapshot)
    data = snapshot.data
    summary = {
        "operation": "warehouse-extract-candidate", "snapshot_id": snapshot.snapshot_id,
        "result_checksum": snapshot.result_checksum, "nonproduction": data["nonproduction"],
        "covered_months": data["requested_months"], "fact_count": len(data["facts"]),
        "city_count": len({r["city_key"] for r in data["facts"]}), "staged_file": str(staged_path),
        "known_subtotal_comparison": (
            known_subtotal_comparison(snapshot, months[-2], months[-1]) if len(months) >= 2 else None
        ),
        "csv_reconciliation": None,
    }
    csv_path = Path(args.reconcile_csv) if args.reconcile_csv else DEFAULT_REFERENCE_CSV
    if csv_path.is_file():
        summary["csv_reconciliation"] = reconcile_with_csv(snapshot, csv_path)
    return summary


def main(argv=None) -> int:
    parser = SafeParser(description="IRF two-month warehouse candidate extractor and publisher.")
    parser.add_argument("--month", help="start ended calendar month, YYYY-MM; Asia/Kolkata")
    parser.add_argument("--end-month", help="optional end calendar month, YYYY-MM")
    parser.add_argument("--extract-candidate", help="stage a validated monthly snapshot candidate to this path")
    parser.add_argument("--publish-candidate", help="publish a staged candidate JSON file to Postgres via GROWTHOS_WRITER_DSN")
    parser.add_argument("--run-id", help="optional snapshot identifier")
    parser.add_argument("--reconcile-csv", help="optional reference CSV path for reconciliation summary")
    parser.add_argument("--authorize-live", action="store_true", help="explicitly authorize bounded warehouse SELECTs and interactive OAuth from your own terminal")
    return execute_cli(lambda: run(parser.parse_args(argv)), max_bytes=MAX_OUTPUT_BYTES, failure_code="WAREHOUSE_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
