# Fantasy Bet Helper

Flags weather, matchup splits, and DraftKings/FanDuel pricing for weekly NFL
fantasy bets, compares your lineup against an optimal one, and surfaces the
stories in a matchup.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how it fits together.

## Quick start

```bash
npm run setup     # once
npm run dev       # http://127.0.0.1:8765
```

That's it. **This is a Python project** — npm is a task runner here and installs
no JavaScript. `npm run setup` builds a virtualenv and installs one dependency
(`requests`); `npm run dev` starts the local server.

| Command | Does |
| --- | --- |
| `npm run dev` | Comp Mode in a browser — upload a lineup, see it marked and compared. Localhost only. |
| `npm run board` | Builds and opens the board: five position cards, flags, narratives, DK/FD toggle |
| `npm run report` | Flag report in the terminal |
| `npm run admin` | Admin console — corrections, narrative curation, slate health |
| `npm run check` | Release gates |

### Without npm

Every script is a one-line wrapper around Python. Python 3.11+, one dependency.

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m sources.serve        # http://127.0.0.1:8765
```

Use `./.venv/bin/python` rather than bare `python`, or activate the venv with
`source .venv/bin/activate`. `admin.py` must be run from the repository root.

## Other entry points

| Command | Does |
| --- | --- |
| `python -m sources.comp lineups/week1.txt` | Compare a lineup from the terminal |
| `python -m sources.comp --optimal --platform fanduel` | Just the optimal lineup |
| `python -m sources.qa` | Data-quality validation; exits 1 on errors |
| `python -m sources.manager --rules` | Validate scoring rules |
| `python sources/developer.py --verify` | Run the release gates |
| `python admin.py narratives --strong` | Every strong story this week |

Offline flags exist on the main pipeline for working without network:
`--no-weather`, `--no-splits`, `--no-rosters`, `--no-pricing`, `--no-narratives`.

## What you need to supply

Most sources are free and need no key. Two things are yours to provide:

**Salary exports** → `salaries/`. Download the contest CSV from a DraftKings or
FanDuel lineup page. Without these there are no value flags and nothing for the
optimizer to work with. See [salaries/README.md](salaries/README.md).

**`ODDS_API_KEY`** → optional. Salary exports carry a season average, so
projections work without it; player props only refine them.

```bash
export ODDS_API_KEY=...        # optional
export NWS_USER_AGENT="fantasy-bet-helper (you@example.com)"
```

The NWS asks for a real contact address. The default is a placeholder.

## Data sources

Weather from the National Weather Service. Splits, rosters, injury reports, and
the schedule from nflverse. Salaries from your contest exports. Everything
downloaded is cached in `.cache/`; delete it to force a refresh.

Manual corrections in `overrides.json` always take precedence over upstream
data — see the admin console.
