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
| `sources/qa.py` | Data-quality validation — `Issue`, `QAReport`, `validate()` |
| `sources/manager.py` | Versioned scoring rules and the dashboard view model |
| `sources/developer.py` | Release gates — secret scan, imports, smoke test — plus commit/push |
| `dashboard.py` | HTML renderer for the view model — **implemented** |
| `overrides.py` | Manual corrections over upstream data — **read side only** |
| `admin.py` | Admin console — the sole write path for corrections |
| `overrides.json` | The corrections themselves, with an audit log |
| `sources/weather.py` | NWS forecast at kickoff — **implemented** |
| `sources/splits.py` | Defensive splits per position, from nflverse — **implemented** |
| `sources/odds.py` | DK/FanDuel salary and projection — **implemented** |
| `sources/injuries.py` | Weekly injury report from nflverse — **implemented** |
| `sources/rosters.py` | Jersey numbers and roster status from nflverse — **implemented** |

### A note on `sources/`

The folder holds two different kinds of module. `weather`, `splits`, `odds`,
`injuries`, and `rosters` are **ingestion** — one per external provider, each
with its own failure mode. `qa`, `manager`, and `developer` are the **agent
roles** from the plan below, which are internal and talk to no provider.

They live together by request. An `agents/` package would keep each folder's
meaning single, and remains the cleaner split if the count grows.

Every module here is importable as `sources.<name>` and runnable either way:

    python -m sources.qa          # idiomatic
    python sources/qa.py          # also works, via a small sys.path bootstrap

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
- **Manual corrections are the source of truth.** An override in
  `overrides.json` always wins over upstream: when it contradicts the feed, when
  the feed is ambiguous, and when the feed omits the player entirely. Partial
  corrections layer field-by-field, taking the rest from upstream. Nothing in
  the codebase may second-guess, soften, or fall back past a correction. QA may
  report on one, never override it. `sources.developer.check_override_precedence`
  guards this on every release.

## External dependencies

| Dependency | Purpose | Failure behavior |
| --- | --- | --- |
| NWS API (`api.weather.gov`) | Kickoff forecast. Free, no key, needs User-Agent | `WeatherUnavailable` → weather flags skipped |
| nflverse (`player_stats_{season}.csv`) | Defensive splits, nflfastR-derived. Free, no key | `SplitsUnavailable` → matchup flags skipped |
| nflverse (`injuries_{season}.csv`) | Official injury report, for QA | `InjuriesUnavailable` → QA warning, pipeline continues |
| nflverse (`roster_{season}.csv`) | Jersey numbers and roster status | `RostersUnavailable` → cards fall back to position |
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

**NFL.com was rejected as a source.** It publishes no developer API, so using
it would mean scraping — brittle, against their terms, and the only such
dependency in an otherwise clean list. nflverse carries jersey numbers and
roster status under the same free, no-auth terms as the splits and injury feeds.

**Two name keys, deliberately.** `normalize_name` collapses to
first-initial-plus-last, which is required to join nflverse's `P.Mahomes` to a
salary export's `Patrick Mahomes`. It is far too lossy for rosters: four players
in the 2026 file reduce to `a.brown`, two of them on Detroit, and the naive
lookup returned Aamaris Brown (#24 DB) for Amon-Ra St. Brown (#14 WR).
`strict_name` keeps the full name and is used wherever both sides have one.
Ambiguous lookups return None rather than guessing — a wrong jersey number
renders confidently, which is worse than a missing one.

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
| **Developer Agent** | Builds and pushes code. **Implemented as `developer.py`.** |
| **Manager Agent** | The UI, and keeping game rules current as platforms change scoring and pricing. **Implemented as `manager.py`.** |

### `sources/qa.py` — the validation layer

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

Run standalone (`python -m sources.qa`, exit 1 on errors) or inline
(`main.py --qa`, `--qa-strict` to abort before the report).

### `sources/manager.py` — rules and presentation

Owns the two things that change for reasons outside this codebase.

**Scoring rules are versioned, not hardcoded.** `RULE_HISTORY` holds each
platform's rules with an effective date; `active_rules(platform, on)` looks them
up by the date being scored. A single current rule set would silently misvalue
every historical comparison the day a platform revises scoring, with no record of
what changed. Entries are append-only — editing one makes past scoring
unreproducible. `validate_rule_sets()` catches duplicate effective dates, invalid
caps, and rule sets unreviewed for three years or more.

**The view model is built here, not in the UI.** `build_view()` returns a
JSON-serializable payload of `PlayerCard`s — initials, number, matchup, accent
colour by worst flag severity, pricing per platform, and any QA issues attached
to that player. A player with an `injury.out` issue comes back `playable: false`.
This keeps presentation out of the rules engine and gives any future dashboard
one contract to consume.

### `admin.py` and `overrides.py` — manual corrections

Upstream feeds carry errors: a wrong jersey number, a stale team after a trade,
a player missing from a roster file. These two modules let those be corrected
without editing source.

**Write authority is centralized at root.** `overrides.py` is read-only — any
module may call `load()`, none may write. Every mutation goes through
`admin.py`, so there is exactly one code path that can change a correction and
exactly one place that records who changed what and why. The console enforces
this literally: it refuses to run outside the repository root, because a
correction applied from a subdirectory would write a second `overrides.json`
that nothing reads, silently doing nothing.

Every mutation prints the before/after and asks for confirmation. `--yes` skips
the prompt for scripted use; nothing skips the audit entry.

Corrections layer over upstream at lookup time and can also *supply* a player the
feed omits entirely. `overrides.validate()` — wired into `qa.py` — reports the
case that matters: a correction upstream has since made **redundant**. That one
is invisible until the feed changes again, at which point it silently overrides a
correct value with a stale one.

| Command | Does |
| --- | --- |
| `python admin.py` | Interactive console |
| `admin.py list` | All active corrections |
| `admin.py inspect "Name"` | Upstream, override, and effective values side by side |
| `admin.py set "Name" --number 14 --note "why"` | Create or replace |
| `admin.py remove "Name"` | Delete |
| `admin.py audit` | Change log — who, when, what |
| `admin.py status` | Slate health: players, flags, QA counts, per-position breakdown |
| `admin.py check` | Validate corrections against upstream |

### `sources/developer.py` — release gates

Owns the path from working tree to pushed commit. Five gates run before anything
is written: forbidden paths, secret scan, module imports, an offline smoke test,
and rule-set validation.

**Pushing is never implicit.** `commit()` and `push()` default to `dry_run=True`
and require an explicit opt-in. A module that can silently publish eventually
publishes something it shouldn't.

The secret scanner exists because it already caught something real — a personal
email hardcoded as a default User-Agent, one command short of being published.
It allowlists deliberate placeholders so it stays quiet in normal use.

## Planned: Dashboard

A player-card grid replacing the CLI printout: dark background, orange-bordered
cards, circular gradient avatar with initials, name, and number. Design is
established; the reference mockup uses NBA players and will be converted to NFL.

Built as `dashboard.py` — `python dashboard.py -o dashboard.html` renders the
view model to one self-contained file. No framework, no external assets.

Presentation lives in the renderer; the view model stays a plain data contract,
so a different front end can consume the same payload without touching it.

Design notes carried from the reference mockup: near-black ground, card edge
coloured by worst flag severity, circular gradient avatar with a gold ring and
initials, condensed uppercase names, large jersey number in brand orange. Two
deliberate departures — the card carries flags, per-platform pricing, and QA
issues the mockup had no room for; and severity drives the card edge, so the
orange border means something rather than being decorative.

**Five cards, one per position group.** Quarterbacks, Running Backs, Wide
Receivers, Kickers, Defense — grouped rather than one tile per player, so the
board reads as a lineup instead of a wall. A group's edge colour takes the worst
severity among its members, so the card summarises what is inside it.

**Sportsbook toggle.** Both platforms' salaries and per-platform flags are
rendered, and a segmented control scopes the view to one at a time. Filtering is
CSS on a root `data-platform` attribute, so switching is instant and the page
stays a single static file with no refetch. Flags carry a `platform` field —
None means the flag is true regardless of book, so weather and matchup flags
show under both.

**Run-level counts live in the admin console, not here.** `admin.py status`
reports players, flags, and QA counts. Someone reading the board wants to know
which players to pick; how many notices the last run produced is operator
information.

**Status: pending design review.** Still not signed off against the original
direction. Open items:

1. **Display face.** The mockup's condensed grotesque is not embedded. Font CDNs
   are blocked in the publish target and no licensed file is vendored, so the
   page uses a heavy system stack with tight tracking. Close in silhouette, not
   the same face. Naming the font and vendoring it as a data URI resolves this.
2. **Card-edge colour encodes severity** rather than being a constant orange
   identity element as in the mockup. If the orange edge should be constant,
   severity needs another home.

`manager.build_view()` is unaffected by any of these — the payload is a data
contract, so a redesign touches `dashboard.py` only.

## Build order

1. ~~`models.py`~~ — done
2. ~~`sources/weather.py`~~ — done
3. ~~`rules.py` with the weather check~~ — done
4. ~~`sources/splits.py` and its check~~ — done
5. ~~`sources/odds.py`~~ — done
6. ~~QA validation (`qa.py`)~~ — done
7. ~~Developer and Manager modules~~ — done
8. ~~Dashboard — the card grid, consuming `manager.build_view()`~~ — done

## Known gaps

- **Split data lags one season.** nflverse has no published `player_stats_2025`
  or `_2026` asset yet, so `SPLITS_SEASON` defaults to 2024. Bump it (or pass
  `--season`) once the current season is published.
- **Kickers have no roster entry when released.** `roster.not_found` fires for
  them, which is correct but indistinguishable from a misspelling. An
  `admin.py set` correction is the intended workaround.
- **Corrections are trusted absolutely — by design.** The console validates
  shape and reports redundancy, but cannot tell a correct override from a
  confident typo. Fixing a bad correction is the operator's job; the audit log
  in `overrides.json` records what was set, by whom, and when.
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
