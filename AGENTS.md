# GrowthOS Data Pipeline Guide (operator-only)

Workspace governance remains in `../../AGENTS.md`; before significant development read
`../../docs/deliverables.md`; before design read `../../docs/adr/README.md`; before
business changes read `../../docs/decisions.md`. Approved scope: `../../docs/specs/v1-spec.md`.

This repo is the only writer. It extracts reviewed monthly aggregates from the
company warehouse and publishes a validated snapshot to Neon. Analysts run the
separate `gwos-analyst` repo and never run this code.

## Fast Command Router

Run from `gwos-data/`. Install the consumer package first with
`pip install -e ../gwos-analyst`, then `pip install -e '.[warehouse,postgres]'`.

| Task | Command (`python3 -m gwosdata.warehouse_cli ...`) |
| --- | --- |
| Offline query plan (no network) | `--month 2026-09 --end-month 2026-10` |
| Live two-month extraction (operator terminal) | `--month 2026-09 --end-month 2026-10 --extract-candidate data/candidates/sep-oct-2026.json --authorize-live` |
| Publish a staged candidate to Neon | `--publish-candidate data/candidates/sep-oct-2026.json` |

## Safety Boundaries

- Live warehouse extraction requires the human operator's own terminal and reads `gwos-data/.env`; never ask users to paste tokens or passwords in chat.
- Publication requires the separate writer connection (`GROWTHOS_WRITER_DSN`). Keep warehouse and writer secrets out of the analyst/reader environment.
- A failed or cancelled refresh preserves the previous verified snapshot; publication is transactional and fenced by generation.
