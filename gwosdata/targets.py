"""Detached target storage contract and packaged schema."""

from dataclasses import dataclass
from importlib.resources import files
import json

from .classification import normalize_city_key
from .contracts import MAX_FACTS, _digest, canonical_json, exact_fields, month, reference, text
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
