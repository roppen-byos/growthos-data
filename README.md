# GrowthOS Data Pipeline (operator-only)

Warehouse extraction and Neon publication for the GrowthOS IRF reporting snapshot. This repository is the only writer: it extracts reviewed monthly aggregates from the company warehouse (Trino), validates a candidate snapshot, and activates it transactionally in Neon Postgres. Analysts never run this code; they read the published snapshot from the separate `gwos-analyst` repository.

## Dependency

This package imports the contract layer (`growthos.contracts`, `growthos.classification`, `growthos.errors`, `growthos.analysis`) from the consumer repo. Install it first:

```bash
git clone <gwos-analyst>   # sibling checkout
python3 -m venv .venv
.venv/bin/python -m pip install -e ../gwos-analyst
.venv/bin/python -m pip install -e '.[warehouse,postgres]'
```

## Operator Commands

From `gwos-data/`:

```bash
# Offline plan (no network): list the queries that would run
.venv/bin/python -m gwosdata.warehouse_cli --month 2026-09 --end-month 2026-10

# Live two-month extraction with interactive OAuth from your own terminal
.venv/bin/python -m gwosdata.warehouse_cli --month 2026-09 --end-month 2026-10 \
  --extract-candidate data/candidates/sep-oct-2026.json --authorize-live

# Publish a validated staged candidate to Neon
.venv/bin/python -m gwosdata.warehouse_cli --publish-candidate data/candidates/sep-oct-2026.json

# Test suite
.venv/bin/python -m unittest discover -s tests -v
```

## Implemented Boundary

| Module | Responsibility |
| --- | --- |
| `gwosdata/warehouse_client.py` | Pinned `trino==0.336.0` transport, repository `.env` loader, WSL2 OAuth prompt, and canonical-city monthly SQL queries (`DEC-005`, `DEC-006`). |
| `gwosdata/warehouse_cli.py` | Single-OAuth multi-month (`2026-09` & `2026-10`) extractor, atomic candidate staging, known-subtotal comparison, CSV reconciliation, candidate loading, and publication CLI. |
| `gwosdata/postgres.py` | Writer connection, session advisory locking, generation fencing, and transactional snapshot activation. |

## Environment Prerequisites

- **Warehouse extraction (`gwos-data/.env`):** `GROWTHOS_TRINO_HOST`, `GROWTHOS_TRINO_USER`, `GROWTHOS_TRINO_CATALOG=hive`, `GROWTHOS_TRINO_PORT=443` (or `TRINO_*` equivalents). Never store tokens or passwords in `.env`.
- **Publication writer (`GROWTHOS_WRITER_DSN`):** a dedicated writer login with `SELECT`/`INSERT`/`UPDATE` on `irf.refresh_runs`/`irf.active_snapshot` plus `SELECT`/`INSERT` on `irf.snapshots`/`irf.facts`. Apply `sql/001_reporting.sql` from the `gwos-analyst` repo as schema owner on an approved empty Postgres database.

## Validation Evidence

- **Unit & config suite:** `.venv/bin/python -m unittest discover -s tests -v` — warehouse transport/config extraction and candidate staging pass without a live connection.
- **Postgres integration suite:** `.venv/bin/python scripts/test_postgres_local.py` — runs the full suite against an isolated disposable local Postgres 17.11 container (reader/writer grants, rollback, fencing, recovery).
