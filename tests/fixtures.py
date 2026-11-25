"""Synthetic aggregate observations; tests only, never a live-source fallback."""

from gwosdata.contracts import FUNNEL_VOLUMES, VOLUMES, MonthlyData, validate_data


def demo_payload() -> dict:
    observations = (
        ("2026-08", "harbor", "Harbor", "T1", (100, 40, 20, 10, 8, 4)),
        ("2026-08", "vale", "Vale", "T3", (0, 0, 0, 0, 0, 0)),
        ("2026-09", "harbor", "Harbor", "T1", (100, 50, 30, 15, 12, 6)),
        ("2026-09", "vale", "Vale", "T3", (300, 30, 10, 5, 4, 3)),
        ("2026-10", "harbor", "Harbor", "T1", (200, 80, 40, 16, 20, 8)),
        ("2026-10", "vale", "Vale", "T3", (100, 20, 20, 12, 8, 5)),
    )
    months = ["2026-08", "2026-09", "2026-10"]
    return {
        "nonproduction": True, "months": months,
        "month_metadata": [
            {
                "month": m, "extracted_at": "2026-11-02T00:01:00+00:00", "business_timezone": "Asia/Kolkata",
                "source_reads": [
                    {"source": source, "month": m,
                     "started_at": f"2026-11-02T00:00:{i * 10:02d}+00:00",
                     "completed_at": f"2026-11-02T00:00:{i * 10 + 5:02d}+00:00"}
                    for i, source in enumerate(("installs", "registrations", "ftu", "funnel"))
                ],
            } for m in months
        ],
        "facts": [
            {"month": m, "city_key": key, "city": city, "tier": tier, "source_cities": [city],
             **dict(zip(VOLUMES, counts)), **dict.fromkeys(FUNNEL_VOLUMES, 0)}
            for m, key, city, tier, counts in observations
        ],
    }


def demo_data() -> MonthlyData:
    return validate_data(demo_payload())
