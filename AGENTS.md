# GrowthOS Data Pipeline Guide (operator-only)

Workspace governance remains in `../../AGENTS.md`; before significant development read `../../docs/worklog.md`; before design read `../../ARCHITECTURE.md` and `../../docs/adr/`; before business changes read `../../docs/business-rules.md`. Approved scope: `../../docs/specs/v1-spec.md`.

This repo is the only writer. It extracts reviewed monthly aggregates (core IRF and funnel metrics) from the company warehouse and atomically replaces requested months in Neon. Analysts run the separate `gwos-analyst` repo and never run this code.

## Fast Command Router

Run from `gwos-data/` using `.venv/bin/python` (or `../.venv/bin/python` when using the workspace's shared venv). Install the consumer package first with `pip install -e ../gwos-analyst`, then `pip install -e '.[warehouse,postgres]'`.

| Task | Command |
| --- | --- |
| Offline query plan (no network/settings) | `.venv/bin/python -m gwosdata.warehouse_cli --month 2026-09 --end-month 2026-10` |
| Fetch and immediately replace months (operator terminal) | `.venv/bin/python -m gwosdata.warehouse_cli --month 2026-06 --end-month 2026-08 --authorize-live` |
| Validate target CSV offline (no settings/network) | `.venv/bin/python -m gwosdata.import_targets --csv PATH --metric platform_ftu\|registrations --check-only` |
| Owner Platform FTU or registration target import (explicit approval required) | `.venv/bin/python -m gwosdata.import_targets --csv PATH --metric platform_ftu\|registrations --authorize-import` |
| Owner-only additive funnel metric columns (explicit approval required) | `.venv/bin/python -m gwosdata.migrate_funnel_metrics --authorize-migration` |
| One-time owner target migration from legacy `irf.pftu_targets` to `irf.targets` (explicit approval required) | `.venv/bin/python -m gwosdata.migrate_targets --authorize-migration` |
| Owner-only migration from legacy snapshot schema (explicit approval required) | `.venv/bin/python -m gwosdata.migrate_monthly --authorize-migration` |
| Owner-only T4-to-T3 migration of current monthly data (explicit approval required) | `.venv/bin/python -m gwosdata.migrate_tiers --authorize-migration` |
| Owner-only removal of legacy version provenance (explicit approval required) | `.venv/bin/python -m gwosdata.migrate_versions --authorize-migration` |

## Safety Boundaries

- **Credentials & terminal:** Live warehouse extraction requires the human operator's interactive terminal and reads `gwos-data/.env`; never ask users to paste tokens or passwords in chat, and never source `.env` into the shell.
- **Secret isolation:** The parent reads `GROWTHOS_WRITER_DSN` from the environment or `gwos-data/.env` after the clean warehouse child exits. Keep database/provider secrets out of that child and all operator secrets out of the analyst environment.
- **Atomic replacement:** `warehouse_cli` extracts all four required monthly sources (`installs`, `registrations`, `ftu`, `funnel`) across the full requested range into memory before writing. In one transaction, it deletes and replaces all requested months (including removing stale cities) while leaving untouched months and their metadata intact. No JSON staging, historical versions, locks, run tracking, retries, or resume — restart failed requests from scratch. On older databases, apply `migrate_funnel_metrics` before refreshing months.
- **Availability & provenance:** Store source-read timestamps without completeness/acceptance labels or version identifiers. A successful fetch and technical validation makes ended months immediately reportable. Definition changes apply to refreshed months only and do not trigger rejection or recalculation of untouched months.
- **Owner migrations & target imports:** All owner migrations and target imports read `.env.owner` (or `GROWTHOS_OWNER_DSN`) only after explicit user authorization. Target installation is separate from warehouse refresh; follow [Target storage and import](README.md#target-storage-and-import) for validation, reader grants, no-overwrite reruns, and the one-time September/October merged-city review (returned for `--metric platform_ftu` imports only). Finish legacy tier/version migrations (`migrate_tiers`, `migrate_versions`) before installing targets or funnel columns, as their exact schema guards intentionally reject expanded tables.

## Verification

Never run integration tests against Neon. Run offline tests and disposable-Postgres integration tests locally from `gwos-data/`:

```bash
# Offline suite (skips disposable-Postgres tests when GROWTHOS_TEST_ALLOW_DDL is unset)
.venv/bin/python -m unittest discover -s tests -v

# Local disposable Postgres integration suite (creates and removes an owned loopback container)
.venv/bin/python scripts/test_postgres_local.py
```

If you modify any of the three core IRF extraction queries (`installs-monthly`, `registrations-monthly`, `ftu-monthly`) in `gwosdata/warehouse_client.py`, update `../gwos-analyst/growthos/sql/warehouse_queries.sql` to match; `tests/test_query_parity.py` fails closed on SQL or checksum drift.
