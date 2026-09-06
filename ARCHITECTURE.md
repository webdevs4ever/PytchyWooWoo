# Architecture

Fantasy Bet Helper — flags weather, matchup splits, and pricing signals for
weekly NFL fantasy bets and parlays on DraftKings and FanDuel.

Derived from `handoff2.md`, which remains the spec of record.

## Bird's eye view

Three layers, in dependency order:

1. **Ingestion** (`sources/`) — one module per provider, because each has its own
   reliability profile and failure mode.
2. **Normalization** (`models.py`) — every source maps its response into shared
   dataclasses, so provider changes are absorbed at the edge.
3. **Rules** (`rules.py`) — independent check functions over a `PlayerSlate`,
   each returning `Flag`s with a human-readable reason.

Output is a CLI report today. A dashboard is planned (see below).

## Code map

| Path | Responsibility |
| --- | --- |
| `main.py` | CLI entry point; assembles slates, runs checks, prints the report |
| `config.py` | Thresholds, API settings, salary caps, 32 stadium coordinates |
| `models.py` | `Player`, `Game`, `Venue`, `WeatherCondition`, `MatchupSplit`, `Pricing`, `StatLine`, `ScoringRules`, `Flag`, `PlayerSlate` |
| `rules.py` | `check_*` functions and `evaluate()` |
| `qa.py` | Data-quality validation — `Issue`, `QAReport`, `validate()` |
| `sources/weather.py` | NWS forecast at kickoff — **implemented** |
| `sources/splits.py` | Defensive splits per position, from nflverse — **implemented** |
| `sources/odds.py` | DK/FanDuel salary and projection — **implemented** |
| `sources/injuries.py` | Weekly injury report from nflverse — **implemented** |

## Cross-cutting concerns

**Configuration.** All tunables live in `config.py`. Secrets come from the
environment (`ODDS_API_KEY`, `NWS_USER_AGENT`) and are never committed.

**Failure handling.** Sources raise a module-specific exception
(`WeatherUnavailable`, `SplitsUnavailable`, `OddsUnavailable`). Callers catch it
and leave the corresponding field `None`. A check with no data returns no flags,
so one dead provider degrades the report rather than breaking the run.

**Rate limits.** The Odds API free tier allows ~500 requests/month, so pricing
must be cached rather than fetched per player. Weather is already cached per
game rather than per player.

## Invariants

- Sources return normalized models, never raw provider payloads.
- Checks are pure functions of a `PlayerSlate` — no I/O, no shared state.
- Absent data yields zero flags. It is never treated as a negative signal.
- Pricing is always keyed by `Platform`. DK and FanDuel differ in salary, cap,
  and scoring, so nothing may assume one shared price.
- Dome venues skip the weather call entirely.

## External dependencies

| Dependency | Purpose | Failure behavior |
| --- | --- | --- |
| NWS API (`api.weather.gov`) | Kickoff forecast. Free, no key, needs User-Agent | `WeatherUnavailable` → weather flags skipped |
| nflverse (`player_stats_{season}.csv`) | Defensive splits, nflfastR-derived. Free, no key | `SplitsUnavailable` → matchup flags skipped |
| nflverse (`injuries_{season}.csv`) | Official injury report, for QA | `InjuriesUnavailable` → QA warning, pipeline continues |
| The Odds API | Player props → projections. Free tier ~500 req/month | Falls back to salary-export season averages |
| DK / FD contest CSV exports | Salaries and salary caps. User-downloaded | No export → no value flags for that platform |

## Decisions and trade-offs

**Odds and DFS salaries are two different products.** `handoff2.md` treats
"DK/FanDuel odds & pricing" as one data need served by one aggregator. It isn't.
DraftKings *Sportsbook* publishes betting odds, which aggregators license and
which yield usable projections via player props. DraftKings *DFS* publishes
contest salaries and caps, which no aggregator carries because they are not
odds. The two are ingested separately.

**Salaries come from the contest CSV export.** Both sites let a logged-in user
download the salary file from a contest's lineup page. That is user-initiated,
needs no credentials in code, and raises none of the ToS problems scraping does.
The export also carries a season average, so pricing works with no API key at
all — props only refine the projection.

**Projections are scored, not read.** Props give raw stat lines; `ScoringRules`
converts them under each platform's own system. The same 6.5 rec / 104.5 yd line
is 23.55 on DraftKings and 17.30 on FanDuel — full vs half PPR plus DK's
100-yard bonus. Reading a single "projected points" number from one source would
erase exactly the divergence Comps Mode exists to find.

**Retractable roofs count as domes.** When closed the forecast is moot, and
treating them as indoor is the safer default for flagging.

**Weather first, odds last.** NWS needs no auth, so it proves the whole pipeline
cheaply. Pricing is the least reliable source and is expected to need the most
iteration.

**Splits are computed here, not fetched pre-aggregated.** nflverse publishes one
row per player per week including `opponent_team`, so fantasy points allowed by
each defense is a group-by away. That avoids depending on someone else's
aggregation choices and makes the PPR/standard switch a one-line config change.

**No pandas.** A single group-by does not justify the dependency; the stdlib
`csv` module handles a few-megabyte season file fine.

**Season files are cached to `.cache/`.** Each is several MB, and re-downloading
per run would be wasteful and rude to the host.

## Planned: Comps Mode

Optimize separately for each platform rather than producing one shared lineup:
pull salaries per site, apply each site's scoring rules, respect each site's
salary cap, and surface players whose value diverges between them.
`check_platform_divergence` in `rules.py` is the groundwork — it already flags
those players. Blocked on `sources/odds.py`.

## Planned: Agents

**Sequenced after `sources/odds.py`, which is now complete — so this is
unblocked.** Until pricing landed, the build order was sequential and a single
agent was the right call; `handoff2.md` makes that argument and it held. The
work now splits into genuinely independent workstreams, which is the condition
that same document sets for adding agents.

Three roles:

| Agent | Owns |
| --- | --- |
| **QA Agent** | Verification. Current player alerts, injury status, and empty or malformed stat sets that would break the UI. **Implemented as `qa.py`** — a plain validation module, not a scheduled agent. Scheduled runs and CI wrapping come later. |
| **Developer Agent** | Builds and pushes code. |
| **Manager Agent** | The UI, and keeping game rules current as platforms change scoring and pricing. |

### `qa.py` — the validation layer

`Flag` and `Issue` are deliberately different things. A `Flag` is a betting
signal the user should weigh. An `Issue` is a defect: missing, stale, or
incoherent data that would render wrong, break a UI, or silently suppress a flag
the user expected. The rules engine stays quiet when data is absent by design —
this module is what makes that silence visible.

Three levels: `ERROR` (would render wrong; `QAReport.ok` is False),
`WARNING` (degraded output worth knowing about), `NOTICE` (expected absence,
recorded for transparency).

Checks return issues rather than raising, so one bad player never aborts
validation of the rest of the slate. Categories:

- **Integrity** — player on neither team, duplicate entries, kickoff in the past
  or beyond the NWS horizon
- **Completeness** — missing weather, split, or pricing, distinguishing expected
  absence (kickers have no splits) from a failed lookup
- **Soundness** — zero or over-cap salary, zero projection against a real salary,
  forecast values outside physical range
- **Availability** — injury designations from the official report
- **Environment** — stale `SPLITS_SEASON`, missing API key, empty `salaries/`

The most valuable check is `unsound.projection_zero`. A zero projection against a
real salary is not missing data, so the value check runs and confidently reports
the worst value on the board. That is a wrong answer rather than an absent one,
which is exactly the class of defect this layer exists to catch.

Run standalone (`python qa.py`, exit 1 on errors) or inline (`main.py --qa`,
`--qa-strict` to abort before the report).

## Planned: Dashboard

A player-card grid replacing the CLI printout: dark background, orange-bordered
cards, circular gradient avatar with initials, name, and number. Design is
established; the reference mockup uses NBA players and will be converted to NFL.
Blocked on nothing — it can proceed in parallel once the CLI output stabilizes.

## Build order

1. ~~`models.py`~~ — done
2. ~~`sources/weather.py`~~ — done
3. ~~`rules.py` with the weather check~~ — done
4. ~~`sources/splits.py` and its check~~ — done
5. ~~`sources/odds.py`~~ — done
6. ~~QA validation (`qa.py`)~~ — done
7. Developer and Manager agents — next

## Known gaps

- **Split data lags one season.** nflverse has no published `player_stats_2025`
  or `_2026` asset yet, so `SPLITS_SEASON` defaults to 2024. Bump it (or pass
  `--season`) once the current season is published.
- **Kickers and defenses have no splits.** They are absent from the weekly
  player stats used here, so `fetch_split` returns None for them and matchup
  flags are silently skipped.
- **Props are untested against the live API.** `ODDS_API_KEY` is unset, so the
  prop-fetching path has never run against a real response — only the parsing
  and scoring around it are verified. Expect iteration on first real call.
- **Touchdown props are not modeled.** `player_anytime_td` returns a probability,
  not a count, and allocating it between rushing and receiving is ambiguous.
  Projections currently carry yards, receptions, and passing TDs only, so
  TD-dependent players are undervalued.
- **Salary exports are manual.** Someone has to download the weekly CSVs into
  `salaries/`. Without them there are no value flags.
