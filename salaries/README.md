# Salary exports

Drop DraftKings and FanDuel contest CSVs here. Both sites let a logged-in user
download the salary file from a contest's lineup page — no scraping involved.

- **DraftKings** — "Export to CSV" on the draft screen. Lands as `DKSalaries.csv`.
- **FanDuel** — "Download players list" on the entry screen. Lands as
  `FanDuel-NFL-<date>-<id>-players.csv`.

Platform is detected from the column headers, so filenames don't matter. Any
`*.csv` in this directory is loaded.

Contents are gitignored: they are week-specific and go stale immediately.
