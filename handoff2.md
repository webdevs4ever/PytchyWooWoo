# Fantasy Bet Helper — Project Handoff

## Overview
A standalone app (separate from the fantasy draft-ranking tool) that flags weather conditions, bad/good matchup splits, and pricing signals for weekly fantasy football bets/parlays on DraftKings/FanDuel.

## Data Source Research

Neither **DraftKings** nor **FanDuel** publishes an official public developer API. Both treat player pricing/odds as proprietary data, available only through private B2B/affiliate partnerships — no self-serve API key exists for individual developers.

Realistic options per data type:

| Data need | Source | Notes |
|---|---|---|
| Weather | NWS API (free, official) or OpenWeatherMap (backup) | No auth needed for NWS; easiest to start with |
| Matchup/defensive splits | nflfastR (free, historical play-by-play) or Pro Football Reference | Legit, no ToS issues |
| DK/FanDuel odds & pricing | Third-party aggregators: The Odds API (free tier ~500 req/month), OddsJam, OpticOdds (paid) | Aggregators use licensed feeds |
| DK/FanDuel odds & pricing (alt.) | Unofficial/reverse-engineered endpoints or scrapers (e.g. Apify actors) | Breaks without notice, violates ToS — treat as fallback only |

## Proposed Architecture

**1. Data ingestion layer** — one module per source, since each has different reliability:
- `sources/weather.py` — NWS API calls
- `sources/splits.py` — matchup/defensive split stats
- `sources/odds.py` — DK/FanDuel pricing via an aggregator API

**2. Normalization layer**
Maps each source's differing response shape into shared internal models (`Player`, `Game`, `WeatherCondition`, `MatchupSplit`). Insulates the rest of the app when a data source changes its API.

**3. Flagging/rules engine** (`rules.py`)
Independent check functions per player-game, each returning a flag + reason string:
- `check_weather(game)` → flag high wind (>15mph), precip, extreme cold — matters most for kickers/passing games
- `check_matchup_split(player, opponent)` → flag opponent allowing well above/below average fantasy points at that position
- `check_price_value(player, price)` → flag under/overpriced relative to projection

**4. Output**
Start with a CLI printout or CSV; a dashboard can come later.

## Suggested Folder Structure

```
fantasy-bet-helper/
├── main.py # CLI entry point
├── config.py # API keys, stadium coords, thresholds
├── sources/
│ ├── weather.py # NWS API calls
│ ├── splits.py # matchup/defensive split data
│ └── odds.py # DK/FanDuel pricing via Odds API
├── models.py # Player, Game, Flag dataclasses
├── rules.py # the check_* flagging functions
└── requirements.txt
```

## Suggested Build Order

1. `models.py` — define data shapes first, before any API calls
2. `sources/weather.py` — easiest API (no auth), proves the pipeline end-to-end
3. `rules.py` with just the weather check
4. Add `sources/splits.py` and its check
5. Add `sources/odds.py` last — least reliable source, expect the most iteration

## On Using AI Coding Agents to Build This

**One agent is the right call, not multiple.** The build order above is mostly sequential (models → weather → splits → odds → rules → output) — each piece depends on the last, so parallel agents don't save much time here and add coordination overhead (merging conflicting file changes, keeping data shapes consistent across modules).

**Realistic timeline with one agent, working interactively:**
- Scaffolding + models + weather integration: ~30–60 min
- Splits data + first rules: ~1–2 hours
- Odds/pricing integration: ~1–2 hours (expect back-and-forth — the unofficial endpoint or aggregator API may not behave exactly as documented)
- Debugging against real data: variable — often the longest part

**Total: a few hours across one or two sessions.**

**When multiple agents would make sense:** only if the work splits into truly independent workstreams — e.g., one agent building the CLI/rules engine while a second builds a separate DK-pricing scraper that runs independently. For a first working version, one agent following the build order above is simpler and easier to review while re-learning the codebase.

## Comps Mode

Instructions for Claude: when operating in "Comps Mode," optimize lineups based on both DraftKings and FanDuel — accounting for each platform's own salary cap, player pricing, and scoring rules (they differ between the two sites). This means:

- Pull player salaries/pricing separately per platform (via `sources/odds.py` or the relevant aggregator/scraper for each site)
- Apply each platform's own scoring rules (e.g. PPR/half-PPR differences, bonus thresholds) when calculating projected value
- Generate a separate optimal lineup for DraftKings and for FanDuel under each site's own salary cap constraint, rather than one shared lineup
- Surface where the two platforms diverge — e.g. a player who's a strong value play on one site but not the other, due to pricing differences
- Respect the flags from the rules engine (weather, matchup splits, price value) as constraints/inputs to the optimization, not just informational flags

## Note on Production Hosting (if this grows beyond personal use)
For an audience of ~1,000 users, this is a small-scale hosting question, not an "agents" question:
- 1 app server process is enough (1k users is light load)
- 1 scheduled background worker to pull weather/odds/splits data periodically
- A cache/DB layer so the app isn't hitting rate-limited APIs (e.g. The Odds API free tier) per user request

Data (weather, odds, splits) is shared across all users rather than personalized per-request, so this scales comfortably without multiple agents or servers.

## Note on the Dashboard Mockup

The reference card-grid mockup for the eventual dashboard currently uses **NBA
players** (LeBron James, Stephen Curry, Kevin Durant, Giannis Antetokounmpo,
Luka Dončić). **These need to be converted to NFL names** before the dashboard
is built — this project is NFL-only, and leaving basketball names in the
reference risks them being carried into real components.

The visual design itself is settled and should be kept as-is: dark background,
orange-bordered cards, circular gradient avatar with initials, name in condensed
uppercase, and a large orange jersey number. Only the player identities change.
