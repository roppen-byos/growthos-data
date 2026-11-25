"""Detached target storage contract and the one-time merged-city import review."""

from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
import json

from .classification import normalize_city_key
from .contracts import MAX_FACTS, MonthlyData, _digest, canonical_json, exact_fields, month, reference, text
from .errors import reject

TARGET_FIELDS = {
    "month", "kind", "city_key", "city", "platform_ftu_target", "registrations_target",
}
TARGET_DB_FIELDS = tuple(sorted(TARGET_FIELDS))
SPECIAL_LABELS = {"overall": "Grand Total", "new_launch": "New launch"}
METRICS = ("platform_ftu", "registrations")


def target_schema_sql() -> str:
    return files("gwosdata").joinpath("sql/002_targets.sql").read_text(encoding="utf-8")


def target_count(value: object) -> bool:
    return value is None or (type(value) is int and 0 <= value <= 2**63 - 1)


@dataclass(frozen=True)
class TargetMonth:
    payload: bytes
    result_checksum: str

    @property
    def data(self) -> dict:
        return json.loads(self.payload)


def validate_targets(raw: object) -> TargetMonth:
    exact_fields(raw, {"month", "rows"})
    month(raw["month"])
    rows = raw["rows"]
    if not isinstance(rows, list) or not 3 <= len(rows) <= MAX_FACTS:
        reject("BOUNDS", "Targets require bounded city, fallback and overall records.")
    seen, specials = set(), set()
    for row in rows:
        exact_fields(row, TARGET_FIELDS)
        kind, key = row["kind"], row["city_key"]
        if row["month"] != raw["month"] or kind not in ("city", *SPECIAL_LABELS):
            reject("TARGETS", "Invalid target month or record kind.")
        if (not text(row["city"]) or not target_count(row["platform_ftu_target"])
                or not target_count(row["registrations_target"])):
            reject("TARGETS", "Targets must have bounded labels and nonnegative 64-bit counts or null.")
        if kind == "city":
            if not reference(key) or normalize_city_key(key) != key:
                reject("TARGETS", "Invalid canonical target city.")
        elif key != "" or row["city"] != SPECIAL_LABELS[kind]:
            reject("TARGETS", "Overall and fallback targets must be distinct from city targets.")
        else:
            specials.add(kind)
        grain = kind, key
        if grain in seen:
            reject("DUPLICATE_GRAIN", "Duplicate canonical target; resolve the source rows explicitly.")
        seen.add(grain)
    if specials != set(SPECIAL_LABELS) or not any(r["kind"] == "city" for r in rows):
        reject("TARGETS", "Each target month needs cities, one New launch and one Grand Total.")
    normalized = sorted(rows, key=lambda r: (r["kind"], r["city_key"]))
    return TargetMonth(canonical_json({**raw, "rows": normalized}), _digest(normalized))


def targets_from_records(records: list[dict]) -> TargetMonth:
    """Validate one bounded SQL result; stored rows carry no separate provenance."""
    if not records:
        reject("TARGETS_UNAVAILABLE", "Targets are unavailable for the requested month; no substitute was selected.")
    if len(records) > MAX_FACTS:
        reject("BOUNDS", "Too many target records for one month.")
    first = records[0]
    for row in records:
        exact_fields(row, set(TARGET_DB_FIELDS))
        if row["month"] != first["month"]:
            reject("STORE_INTEGRITY", "Stored target records disagree on their month.")
    return validate_targets({"month": first["month"], "rows": records})


def target_city_review(dataset: MonthlyData, targets: TargetMonth, city_key: str) -> dict:
    """Preserve the one-time import review without importing the analyst engine."""
    data, plan = dataset.data, targets.data
    period = plan["month"]
    target = next((r for r in plan["rows"] if r["kind"] == "city" and r["city_key"] == city_key), None)
    actual = next((r for r in data["facts"] if r["month"] == period and r["city_key"] == city_key), None)
    target_value = target["platform_ftu_target"] if target else None
    actual_value = actual["platform_ftu"] if actual else None
    variance = None if target_value is None or actual_value is None else actual_value - target_value
    reason = "missing_target_or_actual" if variance is None else "nonpositive_target" if target_value <= 0 else None
    return {
        "nonproduction": data["nonproduction"], "result_checksum": dataset.result_checksum,
        "read_at": datetime.now(timezone.utc).isoformat(), "months": data["months"],
        "month_metadata": data["month_metadata"],
        "warnings": (["NONPRODUCTION_SYNTHETIC_DATA"] if data["nonproduction"] else []) + [
            "Current monthly data may be replaced by a later fetch; no historical versions are retained.",
            "Source-read timestamps describe query execution, not authoritative business-data completeness.",
            "Independent warehouse reads are not a transactional warehouse view; retained months are fetched independently.",
        ],
        "month": period, "city_key": city_key, "target": target_value, "actual": actual_value,
        "variance": variance, "variance_pct": 100 * variance / target_value if reason is None else None,
        "above_5_percent": reason is None and 20 * variance < -target_value,
        "unavailable_reason": reason, "target_checksum": targets.result_checksum,
        "interpretation": "One-time merged-city review; separate source-city actuals cannot be reconstructed.",
    }
