# GrowthOS Data Pipeline (operator-only)

Authorized teammates fetch monthly warehouse aggregates and replace those months
in the shared Neon database. Coordinate so only one refresh runs at a time.
This repository is self-contained: no analyst checkout, package, or credentials
file from another repository is required.

## Setup

Use Python 3.11 or newer. Clone this repository and run from its root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[warehouse,postgres]'
```

Create a private `.env` using `.env.example` as the template; preserve any existing
settings. Obtain authorized connection details through your company's approved
credential-sharing channel, not Git or chat. Configure:

- `GROWTHOS_TRINO_HOST`, `GROWTHOS_TRINO_USER`, `GROWTHOS_TRINO_CATALOG=hive`,
  `GROWTHOS_TRINO_PORT=443`. Four `TRINO_*` file aliases are also supported.
- `GROWTHOS_WRITER_DSN`: dedicated writer connection with `sslmode=verify-full`
  and a trusted CA root.
- Store `.env` privately (`0600`); `.env*` is Git-ignored except
  the blank `.env.example` template. Warehouse OAuth
  tokens remain memory-only; each teammate completes their own browser approval.

Do not source the entire operator `.env`:
the command selects settings directly and isolates the warehouse child environment.

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

No JSON handoff files, staged candidates, revisions, run tables, active pointer,
locks, retries or checkpoints. If extraction/validation fails, nothing is written;
if a write transaction fails it rolls back. Restart the entire requested fetch.
A lost database commit acknowledgement can be uncertain: inspect current data
before rerunning. Only one operator refresh may run at a time; concurrency is not
coordinated. A technical validation success does not certify source completeness.

Routine refreshes of an already-configured database need only `.env`. They do not
need owner or reader settings, and do not run migrations or target imports.

## Database schema and migration

Monthly actuals use `irf.facts` (month/city key) and `irf.month_metadata`. The writer needs
`USAGE` on `irf` and `SELECT, INSERT, DELETE` on both tables. The reader gets only
`SELECT`; neither runtime role owns the schema or has DDL privileges.

For a new approved empty database, the schema owner applies
`gwosdata/sql/001_reporting.sql` and grants the two runtime roles. To migrate
the old snapshot schema, after explicit destructive-migration authorization:

```bash
.venv/bin/python -m gwosdata.migrate_monthly --authorize-migration
```

This reads the owner DSN from protected `.env` or `GROWTHOS_OWNER_DSN`, checks
the active data's count/checksum, copies it to current months, rechecks the result,
updates grants and removes the specifically identified legacy tables/functions in
one transaction. Unknown/already-migrated states are rejected; no `CASCADE` is used.
Old inactive revisions are discarded. Routine refresh never runs migrations.

All owner commands use the same local `.env` (environment variables override
the file). Use `.env.example` as a template without
overwriting existing settings:

| Setting | Local file | Purpose |
| --- | --- | --- |
| `GROWTHOS_OWNER_DSN` | `.env` | Connection used for the explicitly authorized owner operation |
| `GROWTHOS_READER_DSN` | `.env` | Identifies the reader login for grants and endpoint checks; no reader connection is opened |
| `GROWTHOS_WRITER_DSN` | `.env` | Identifies the writer login for grants and endpoint checks |

Use distinct owner/writer/reader users on the same host, port, and database, all
with `sslmode=verify-full`. Keep `.env` private (`0600`) and out of Git.
Owner and reader settings are optional for routine warehouse refreshes. Warehouse
configuration selects only Trino settings from this file, and the extraction child
environment excludes database credentials.

### Fold existing T4 cities into T3

Future extraction assigns named unmapped cities to T3 instead of the retired T4.
For an existing **current monthly** database, after explicit owner-migration approval:

```bash
.venv/bin/python -m gwosdata.migrate_tiers --authorize-migration
```

This uses the same protected owner/writer/reader settings and endpoint checks as
the legacy migration. It verifies every stored month's checksum/count, changes
only T4 tier labels to T3, recomputes their checksums, and restricts the database
to T1–T3 in one transaction. Counts, city keys, lineage, source-read times,
publication times, month metadata and unaffected months remain unchanged. No
warehouse fetch occurs. Repeating it is a data no-op. Unknown schema states, corrupt
data or invalid provenance roll back all changes. Run it before deploying the
updated analyst against old T4 data; reader commands do not perform migrations.
Historical local reports/exports are not rewritten.

### Remove legacy version provenance

The current storage contract has no version fields, so stored
metadata that still carries `definition_version`, `mapping_version`, `reference_version`, or
`source_reads[*].query_version` no longer validates. For an existing **current monthly** database,
after explicit owner-migration approval:

```bash
.venv/bin/python -m gwosdata.migrate_versions --authorize-migration
```

This uses the same protected owner/writer/reader settings and endpoint checks as
the legacy migration. It verifies every stored month's checksum/count, then removes
only the legacy version keys from each month's metadata and `source_reads`.
Counts, city keys, lineage, source-read times, publication times and checksums
are unchanged. No warehouse fetch occurs. Repeating it is a data no-op. Unknown
schema states or corrupt data roll back all changes. Run it before deploying the
updated analyst; reader commands do not perform migrations.

### Add funnel metric columns

The reporting schema stores nine additive monthly FTU-eligible funnel columns (`fe_sessions`,
`rr`, `net`, `bike_rr`, `bike_net`, `auto_rr`, `auto_net`, `cab_rr`, `cab_net`)
in `irf.facts`. For an existing database created before these columns existed,
run the owner-only additive migration after explicit approval (and before running
a live warehouse refresh that writes funnel columns):

```bash
.venv/bin/python -m gwosdata.migrate_funnel_metrics --authorize-migration
```

This applies the packaged `gwosdata/sql/003_funnel_metrics.sql` inside one
transaction using the same protected owner settings. Existing rows remain
unchanged with `NULL` funnel values until those months are refreshed, and
historical IRF fact checksums are preserved.

### Target storage and import

Both approved projection CSVs are stored together in `irf.targets`: one row per
month, kind and canonical city carrying `platform_ftu_target` and
`registrations_target`. `irf.target_imports` records presence per metric-month so
an unimported metric is never mistaken for a blank target. No source checksum,
import timestamp, row count or original target components are stored; each import
validates fully inside its transaction and keeps no post-hoc evidence.

Obtain the approved Platform FTU and Registration projection CSVs separately.
They are not bundled with this repository. The examples below use local paths
`data/ftu_targets.csv` and `data/registration_targets.csv`; `data/` is Git-ignored.

If a legacy `irf.pftu_targets` table still exists, migrate it first (owner-only,
explicit approval required). The migration copies it into `irf.targets`, labels
every existing row `platform_ftu`, verifies per-month counts and sums, writes the
`platform_ftu` markers, drops the legacy table and regrants reader SELECT in one
transaction. Unknown or already-migrated states are rejected.

```bash
.venv/bin/python -m gwosdata.migrate_targets --authorize-migration
```

Then import each metric separately:

```bash
# Offline validation only: no settings or database access
.venv/bin/python -m gwosdata.import_targets --csv data/ftu_targets.csv --metric platform_ftu --check-only
.venv/bin/python -m gwosdata.import_targets --csv data/registration_targets.csv --metric registrations --check-only

# Only after separate approval to create/import the live tables
.venv/bin/python -m gwosdata.import_targets --csv data/ftu_targets.csv --metric platform_ftu --authorize-import
.venv/bin/python -m gwosdata.import_targets --csv data/registration_targets.csv --metric registrations --authorize-import
```

The CSV is validated before protected owner settings are read. The import uses the
existing owner/writer/reader endpoint and distinct-login checks, creates both tables
when absent, writes only the selected metric column, re-reads and compares the result
and grants reader SELECT in one transaction. The ordinary writer receives no target
privileges. Facts, month metadata and warehouse queries are unchanged. Future target
months are stored but cannot be analyzed before ending.

Import corrections retain T2-tagged Ludhiana (without retiering actuals), sum
Attingal + Trivandrum targets under Trivandrum, and store Grand Total and New launch
as separate record kinds. Blank source targets stay unavailable, never zero. Grand
Total is authoritative despite detail differences. At read time the single New launch
target pair is compared against the pooled combined actual of all unmatched cities;
it is not repeated per city, added to the national target or allocated during import.

An identical metric rerun is a data no-op; differing stored values or month sets fail
without overwrite. Importing one metric leaves the other metric's column untouched.
Review conflicting data manually rather than dropping the table to force an import.

The Platform FTU import result includes a **one-time September/October 2026 Trivandrum
review** from stored actuals. Registration imports return no merged-city review.
Missing target/actual months are reported as unavailable; separate Attingal actuals
cannot be reconstructed. This introduces no recurring severity workflow and makes no
August target. No warehouse fetch is performed.

Run legacy tier/version migrations **before** target migration or installation: their
exact table allowlists intentionally reject additional tables. Target access is checked
only by the analyst target commands, preserving existing commands before installation.
Live execution remains separate from local implementation/test approval.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/test_postgres_local.py
```

The second command creates and removes an owned loopback-only disposable Postgres
container. Never point the integration tests at Neon. Tests cover month replacement,
stale-city removal, untouched metadata, multi-month rollback, permissions, consistent
reads, migration preservation/corruption rejection, and the guarded warehouse CLI.
The offline suite checks local schemas, historic checksums, reviewed warehouse SQL,
owner-setting locations, and operation without other checkouts or site packages.

## Known limitations

These previously reviewed correctness issues are separate from standalone setup:

- Registration records tied on earliest date across different cities are currently
  selected arbitrarily rather than rejected. The SQL has not changed in this task.
- Legacy tier/version migrations currently expect funnel columns even on older
  databases. Have a maintainer review a legacy upgrade before running it; do not
  bypass schema guards or change migration order to work around the failure.
