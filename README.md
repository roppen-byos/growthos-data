# GrowthOS Data Pipeline (operator-only)

Authorized monthly warehouse aggregates and replace those months in the shared Neon database.

## Setup

Use Python 3.11 or newer. Clone this repository and run from its root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[warehouse,postgres]'
```

Create a private `.env` using `.env.example` as the template; preserve any existing
settings. Configure:

- `GROWTHOS_TRINO_HOST`, `GROWTHOS_TRINO_USER`, `GROWTHOS_TRINO_CATALOG=hive`,
  `GROWTHOS_TRINO_PORT=443`. Four `TRINO_*` file aliases are also supported.
- `GROWTHOS_WRITER_DSN`: dedicated writer connection with `sslmode=verify-full`
  and a trusted CA root.
- Warehouse OAuth tokens remain memory-only.

## Fetch and publish

Run from `gwos-data/` in your own interactive terminal:

```bash
# Offline plan: no settings read or network activity
.venv/bin/python -m gwosdata.warehouse_cli --month 2026-06 --end-month 2026-08

# Fetch June, July, August and publish immediately
.venv/bin/python -m gwosdata.warehouse_cli --month 2026-06 --end-month 2026-08 --authorize-live

# Re-fetch all July data and replace July only
.venv/bin/python -m gwosdata.warehouse_cli --month 2026-07 --authorize-live
```

Open the displayed OAuth URL in your browser. All four required monthly sources
(`installs`, `registrations`, `ftu`, `funnel`) finish and are validated in memory
before Neon is modified. One transaction replaces the complete requested months,
removing stale cities as well as replacing values (including the nine nullable
funnel columns in `irf.facts`). Other months and their metadata remain unchanged.
New data is immediately available to analysts.

## Database schema

Monthly actuals use `irf.facts` (month/city key) and `irf.month_metadata`. The writer needs
`USAGE` on `irf` and `SELECT, INSERT, DELETE` on both tables. The reader gets only
`SELECT`; neither runtime role owns the schema or has DDL privileges.

For a new approved empty database, the schema owner applies
`gwosdata/sql/001_reporting.sql` (already includes funnel columns) and grants the
two runtime roles. Target imports use the current `gwosdata/sql/002_targets.sql`
to create `irf.targets` and `irf.target_imports` when absent.

Only the current schema is supported. Historical upgrade commands are retired;
incompatible existing databases require a separately reviewed change. Routine
refreshes do not run schema setup and need only the writer DSN.

| Setting | Local file | Purpose |
| --- | --- | --- |
| `GROWTHOS_OWNER_DSN` | `.env` | Connection used for the explicitly authorized owner operation |
| `GROWTHOS_READER_DSN` | `.env` | Identifies the reader login for grants and endpoint checks; no reader connection is opened |
| `GROWTHOS_WRITER_DSN` | `.env` | Identifies the writer login for grants and endpoint checks |

Use distinct owner/writer/reader users on the same host, port, and database, all
with `sslmode=verify-full`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/test_postgres_local.py
```

The second command creates and removes an owned loopback-only disposable Postgres
container. Never point the integration tests at Neon. Tests cover month replacement,
stale-city removal, untouched metadata, multi-month rollback, permissions, consistent
reads, target imports, and the guarded warehouse CLI.
The offline suite checks local schemas, historic checksums, reviewed warehouse SQL,
owner-setting locations, and operation without other checkouts or site packages.
