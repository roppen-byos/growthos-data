"""One operator command: fetch a range in memory, then atomically replace it in Neon."""

from __future__ import annotations

from collections import defaultdict
import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys

from .classification import resolve_city
from .contracts import BUSINESS_TIMEZONE, FUNNEL_VOLUMES, VOLUMES, month_range, validate_data
from .errors import MAX_OUTPUT_BYTES, SafeParser, execute_cli, reject
from .postgres import connect_writer
from .warehouse_client import (
    BIKE_SERVICES, ALL_OPERATIONS, OPERATOR_ENV_FILE,
    LANDED_OPERATIONS, MonthWindow, Operation, WarehouseSession, landed_cutoff, operator_environment, query_for,
)

MAX_TRANSFER_BYTES = 16 * 1024 * 1024
SAFE_CHILD_DETAIL = re.compile(
    r"Warehouse [a-z-]{1,40} exceeded its time budget\.|Warehouse extraction exceeded its total time budget\."
    r"|No landed data in the last [0-9]{1,2} days for: [a-z_, ]{1,120}; nothing was published\.")


class WarehouseExtractor:
    def __init__(self, session):
        self.session = session

    def extract(self, months, *, month_to_date=False):
        if not months or months != month_range(months[0], months[-1]):
            reject("PERIOD", "Fetch months must be ordered, unique and contiguous.")
        windows = request_windows(months, month_to_date)
        self.session.authorize(windows[0], extra_windows=tuple(windows[1:]))
        if month_to_date:
            # Cut the current month at the oldest landed day so no source is partially counted.
            probe = windows[-1]
            cutoff = landed_cutoff(probe, {source: self.session.run(operation, probe)["rows"][0]["latest"]
                                           for operation, source in LANDED_OPERATIONS.items()})
            windows[-1] = probe.cut(cutoff)
        cells, samples, reads = {}, defaultdict(set), defaultdict(list)
        for window in windows:
            for source, operation in (("installs", Operation.INSTALLS_MONTHLY),
                                      ("registrations", Operation.REGISTRATIONS_MONTHLY),
                                      ("ftu", Operation.FTU_MONTHLY),
                                      ("funnel", Operation.FUNNEL_MONTHLY)):
                result = self.session.run(operation, window)
                # An empty required source is not evidence of a genuinely zero month.
                if not result["rows"]:
                    reject("COVERAGE", "A required monthly source returned no observations; nothing was published.")
                reads[window.value].append({"source": source, "month": window.value,
                    "started_at": result["started_at"], "completed_at": result["completed_at"]})
                for row in result["rows"]:
                    key = row["canonical_city_key"]
                    samples[(window.value, key)].add(row["sample_source_city"])
                    cell = cells.setdefault((window.value, key), {metric: None for metric in (*VOLUMES, *FUNNEL_VOLUMES)})
                    metrics = FUNNEL_VOLUMES if source == "funnel" else (VOLUMES[2:] if source == "ftu" else (source,))
                    for metric in metrics:
                        cell[metric] = row[metric]
        extracted = datetime.now(timezone.utc).isoformat()
        facts = []
        for (m, key), metrics in sorted(cells.items()):
            _, city, tier, lineage = resolve_city(key, min(samples[(m, key)]))
            facts.append({"month": m, "city_key": key, "city": city, "tier": tier,
                          "source_cities": list(lineage), **metrics})
        return validate_data({"nonproduction": self.session.nonproduction,
            "months": months, "facts": facts,
            "month_metadata": [{"month": w.value, "extracted_at": extracted, "business_timezone": BUSINESS_TIMEZONE,
                "source_reads": reads[w.value],
                **({"cutoff_date": w.through.isoformat()} if w.through is not None else {})} for w in windows]})


def request_windows(months, month_to_date):
    """Ended months, plus the current month as a month-to-date probe when requested last."""
    if not month_to_date:
        return [MonthWindow(m) for m in months]
    return [*(MonthWindow(m) for m in months[:-1]), MonthWindow.month_to_date(months[-1])]


def extraction_environment(environ=None):
    """The warehouse child inherits terminal/config context, never database/provider secrets."""
    env = os.environ if environ is None else environ
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "PYTHONPATH", "VIRTUAL_ENV",
               "GROWTHOS_TRINO_HOST", "GROWTHOS_TRINO_USER", "GROWTHOS_TRINO_CATALOG", "GROWTHOS_TRINO_PORT",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}
    return {key: value for key, value in env.items() if key in allowed}


def extract_live(months, month_to_date=False):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        reject("OPERATOR_TERMINAL", "Live extraction requires the human operator's terminal.")
    config = operator_environment(env_file=OPERATOR_ENV_FILE)
    with WarehouseSession(config, live_authorized=True, interactive=True) as session:
        return WarehouseExtractor(session).extract(months, month_to_date=month_to_date)


def fetch_data(months, month_to_date=False):
    # OAuth stderr/stdin stay attached to the terminal. Only bounded aggregates use
    # the private stdout pipe; no mandatory file or writer DSN enters the child.
    try:
        result = subprocess.run([sys.executable, "-m", "gwosdata.warehouse_cli",
            "--month", months[0], "--end-month", months[-1], "--authorize-live", "--_extract",
            *(["--month-to-date"] if month_to_date else [])],
            env=extraction_environment(), stdout=subprocess.PIPE, timeout=660, check=False)
    except subprocess.TimeoutExpired:
        reject("WAREHOUSE_TIMEOUT", "Extraction timed out; nothing was published. Restart the requested range.")
    if len(result.stdout) > MAX_TRANSFER_BYTES:
        reject("BOUNDS", "Extraction exceeds the aggregate transfer limit; nothing was published.")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        reject("WAREHOUSE_FAILED", "Extraction did not return valid aggregates; nothing was published.")
    if result.returncode:
        # Child errors are already sanitized by execute_cli; never echo arbitrary output.
        code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
        allowed = {"OPERATOR_TERMINAL", "WAREHOUSE_CONFIG", "WAREHOUSE_CONFIG_MISSING", "WAREHOUSE_CONFIG_FILE",
                   "WAREHOUSE_QUERY", "WAREHOUSE_TIMEOUT", "WAREHOUSE_SCHEMA", "COVERAGE", "DEPENDENCY",
                   "NETWORK_POLICY_REVIEW", "CREDENTIAL_BOUNDARY", "WAREHOUSE_FAILED", "BOUNDS"}
        message = payload.get("error", {}).get("message") if isinstance(payload, dict) else None
        if code in ("WAREHOUSE_TIMEOUT", "COVERAGE") and isinstance(message, str) and SAFE_CHILD_DETAIL.fullmatch(message):
            # Relay only fixed-shape timeout/coverage detail naming a registered operation or source.
            reject(code, f"{message} Restart the requested range.")
        reject(code if code in allowed else "WAREHOUSE_FAILED", "Extraction failed; nothing was published. Restart the requested range.")
    dataset = validate_data(payload)
    if dataset.data["months"] != months:
        reject("REQUEST_MISMATCH", "Extracted months differ from the requested range; nothing was published.")
    cut = [m["month"] for m in dataset.data["month_metadata"] if "cutoff_date" in m]
    if cut != (months[-1:] if month_to_date else []):
        reject("REQUEST_MISMATCH", "Extracted month-to-date cutoffs differ from the request; nothing was published.")
    return dataset


def run(args):
    months = month_range(args.month, args.end_month or args.month)
    windows = request_windows(months, args.month_to_date)
    if not args.authorize_live:
        plan = {"operation": "warehouse-refresh-plan", "network_attempted": False,
            "requested_months": months, "bike_services": list(BIKE_SERVICES),
            "queries": [{"month": w.value, "operation": op.value, "query_checksum": query_for(op, w).checksum}
                        for w in windows for op in ALL_OPERATIONS]}
        if args.month_to_date:
            probe = windows[-1]
            plan["month_to_date"] = {"month": probe.value, "latest_possible_cutoff": probe.through.isoformat(),
                "cutoff_rule": "oldest latest-landed day across all sources",
                "lookback_start": probe.lookback_start.isoformat(),
                "landed_probes": [{"operation": op.value, "query_checksum": query_for(op, probe).checksum}
                                  for op in LANDED_OPERATIONS]}
        return plan
    if args._extract:
        return extract_live(months, args.month_to_date).data
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        reject("OPERATOR_TERMINAL", "Live refresh requires the human operator's terminal.")
    dataset = fetch_data(months, args.month_to_date)
    with connect_writer() as store:
        published = store.replace_months(dataset)
    result = {"operation": "warehouse-refresh", "published": True, "replaced_months": months,
            "fact_count": len(published.data["facts"]), "result_checksum": published.result_checksum,
            "other_months": "unchanged"}
    cutoffs = {m["month"]: m["cutoff_date"] for m in published.data["month_metadata"] if "cutoff_date" in m}
    if cutoffs:
        result["month_to_date_cutoffs"] = cutoffs
    return result


def main(argv=None):
    parser = SafeParser(description="Fetch requested months and atomically replace them in Neon; other months stay unchanged.")
    parser.add_argument("--month", required=True, help=f"start ended calendar month YYYY-MM; {BUSINESS_TIMEZONE}")
    parser.add_argument("--end-month", help="optional inclusive end calendar month YYYY-MM")
    parser.add_argument("--month-to-date", action="store_true", help="treat the last requested month as the current month, cut at the oldest landed day (at most yesterday)")
    parser.add_argument("--authorize-live", action="store_true", help="authorize interactive warehouse reads and immediate Neon replacement; omission prints an offline plan")
    parser.add_argument("--_extract", action="store_true", help=argparse.SUPPRESS)
    def execute():
        parsed = parser.parse_args(argv)
        return run(parsed)
    # Parse inside the sanitized CLI wrapper, including malformed arguments.
    transfer = "--_extract" in (sys.argv[1:] if argv is None else argv)
    return execute_cli(execute, max_bytes=MAX_TRANSFER_BYTES if transfer else MAX_OUTPUT_BYTES, failure_code="WAREHOUSE_FAILED")


if __name__ == "__main__":
    raise SystemExit(main())
