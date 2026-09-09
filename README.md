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

| Page | URL |
| --- | --- |
| Comp Mode — upload and analyze | `http://127.0.0.1:8765/` |
| Admin console — manual corrections | `http://127.0.0.1:8765/admin` |

`npm run admin` opens the same corrections in the terminal, with narrative
curation and slate status that the web page does not carry.

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
FanDuel lineup page. See [salaries/README.md](salaries/README.md).

Until you have one, `npm run admin` → `generate-salaries` builds a placeholder
export covering **every active skill player** (567 of them), with projections
derived from the previous season's actual production rather than invented. Real
exports replace it; the prices are the only synthetic part.

Individual players can be corrected or added by hand:

```bash
python admin.py set "James Cook" --salary 9200 --note "underpriced in export"
python admin.py set "Some Rookie" --position WR --team KC --salary 3200 --projection 9.5
python admin.py set "James Cook" --salary 4100 --platform fanduel
```

A correction carrying a salary for someone absent from the export adds them to
the pool outright. Pricing applies to both platforms unless `--platform` scopes
it — or use the dropdown at `/admin`.

**`ODDS_API_KEY`** → optional. Salary exports carry a season average, so
projections work without it; player props only refine them.

```bash
export ODDS_API_KEY=...        # optional
export NWS_USER_AGENT="fantasy-bet-helper (you@example.com)"
```

The NWS asks for a real contact address. The default is a placeholder.

## TODO

1. **Write down specific dates to QA NFL injuries and rosters.** Injury reports
   and roster moves land on a weekly rhythm — final designations Friday,
   inactives 90 minutes before kickoff. Pin the actual dates and times so QA
   runs against the right snapshot instead of whatever happens to be cached.
2. **Figure out the UI with expert picks built in.** Where consensus rankings
   sit on the board without competing with the flags.
3. **Ask the user their favourite team and skin the background in that team's
   colours.**
4. **Connect the GitHub MCP in Claude.** `.mcp.json` already registers the
   server; it has never been authorised. Run `/mcp` and complete the OAuth flow.
5. **Build the lower-right THE DUKE football.**
6. **Tecmo Ballers mode.**
7. **Build out the 1v1 game playing specs and UI.** The head-to-head quiz
   described in ARCHITECTURE.md. Still gated — specs first, then approval,
   then build. Lives with THE DUKE
8. **Delete sensitive IP and audit.** `sources/developer.py` already runs a
   secret scan on every release, and it has caught one real leak (a personal
   email hardcoded as a default User-Agent). Widen it to a full history audit:
   the scan covers the working tree, not past commits, so anything published
   earlier is still in the git history.

## Data sources

Weather from the National Weather Service. Splits, rosters, injury reports, and
the schedule from nflverse. Salaries from your contest exports. Everything
downloaded is cached in `.cache/`; delete it to force a refresh.

Manual corrections in `overrides.json` always take precedence over upstream
data — see the admin console.
