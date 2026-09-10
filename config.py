"""Tunable thresholds, API settings, and stadium coordinates.

Everything a human might want to adjust without reading the rules engine lives
here. Secrets come from the environment, never from this file.
"""

from __future__ import annotations

import os
from pathlib import Path

from models import LineupRules, Platform, Position, ScoringRules, Venue

# --- API access -------------------------------------------------------------

# The NWS API needs no key, but it does require a contact string in User-Agent.
# NWS asks for a contact address in User-Agent. Set NWS_USER_AGENT in your
# environment with a real one; the default is deliberately generic so a personal
# address is never committed.
NWS_USER_AGENT = os.environ.get(
    "NWS_USER_AGENT", "fantasy-bet-helper (contact: set NWS_USER_AGENT)"
)
NWS_BASE_URL = "https://api.weather.gov"

# The Odds API free tier is ~500 requests/month, so cache aggressively.
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_API_SPORT = "americanfootball_nfl"

# Player-prop markets used to derive projections, mapped to StatLine fields in
# sources/odds.py. Each market costs a request against the monthly quota.
ODDS_API_MARKETS = [
    "player_pass_yds",
    "player_pass_tds",
    "player_rush_yds",
    "player_reception_yds",
    "player_receptions",
]

# Free tier is ~500 requests/month, so responses are cached this long.
ODDS_CACHE_TTL_SECONDS = int(os.environ.get("ODDS_CACHE_TTL", str(6 * 3600)))

# DFS salaries are NOT available from the odds API — see sources/odds.py.
# Drop DraftKings/FanDuel contest CSV exports here instead.
SALARY_DIR = Path(os.environ.get("FBH_SALARY_DIR", "salaries"))

REQUEST_TIMEOUT_SECONDS = 15

# nflverse publishes weekly player stats as season CSVs. Free, no key, no ToS
# friction. The most recent season with a published asset is used by default.
NFLVERSE_BASE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/player_stats"
)
SPLITS_SEASON = int(os.environ.get("SPLITS_SEASON", "2024"))

# Injury reports are published under a separate release and appear earlier in
# the year than the stats file, so they track their own season.
NFLVERSE_INJURY_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/injuries"
)
INJURY_SEASON = int(os.environ.get("INJURY_SEASON", "0")) or None

# Below this many rows, a season's injury report has not really been filed yet
# and its silence must not be read as "nobody is hurt".
INJURY_SPARSE_THRESHOLD = int(os.environ.get("INJURY_SPARSE_THRESHOLD", "200"))

# Rosters supply jersey numbers for the dashboard and roster status for QA.
NFLVERSE_ROSTER_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/rosters"
)
ROSTER_SEASON = int(os.environ.get("ROSTER_SEASON", "0")) or None

# How many seasons of roster history Narrative Street diffs. Three covers the
# useful window: older stints stop reading as a grudge.
NARRATIVE_LOOKBACK = int(os.environ.get("NARRATIVE_LOOKBACK", "3"))

# How many seasons of history a market question is answered from.
MARKET_LOOKBACK = int(os.environ.get("MARKET_LOOKBACK", "3"))

# Wikipedia asks for a descriptive User-Agent and expects courteous pacing.
# Text from there is CC BY-SA; attribution appears wherever it is used.
WIKIPEDIA_USER_AGENT = os.environ.get(
    "WIKIPEDIA_USER_AGENT", "fantasy-bet-helper/1.0 (personal research tool)"
)
WIKIPEDIA_DELAY_SECONDS = float(os.environ.get("WIKIPEDIA_DELAY", "2.0"))
# The API accepts up to 50 titles per query; fetching one at a time gets
# rate-limited after about twenty.
WIKIPEDIA_BATCH_SIZE = int(os.environ.get("WIKIPEDIA_BATCH_SIZE", "40"))
# Wikipedia throttles sustained querying even when batched, so bulk sweeps are
# refused rather than retried into a block. Hometown narratives are therefore a
# per-player lookup, not a league-wide scan.
WIKIPEDIA_MAX_BATCHES = int(os.environ.get("WIKIPEDIA_MAX_BATCHES", "3"))

# Strength thresholds. A one-year rental carries no grudge and a move three
# seasons back has gone stale, so a revenge game is only strong when it is both
# recent and earned. Tunable without touching the detection logic.
REVENGE_STRONG_MAX_SEASONS_SINCE = 1   # left last season
REVENGE_STRONG_MIN_TENURE = 2          # after at least two seasons there
REUNION_MIN = 2                        # fewer than this is not reported at all
REUNION_STRONG_MIN = 3                 # this many or more is a strong story
SPLITS_SCORING = os.environ.get("SPLITS_SCORING", "ppr")  # "ppr" or "standard"

# Downloaded season files land here; each is several MB, so they are cached
# rather than re-fetched.
CACHE_DIR = Path(os.environ.get("FBH_CACHE_DIR", ".cache"))

# --- Flagging thresholds ----------------------------------------------------

# Which positions each condition actually affects. Wind degrades the throwing
# and kicking game; rain is a ball-security and footing problem, which lands on
# the players carrying and catching it.
WIND_AFFECTED_POSITIONS = ("QB", "K")
RAIN_AFFECTED_POSITIONS = ("WR", "RB", "TE")

WIND_WARNING_MPH = 15.0       # kicking and deep passing start degrading here
WIND_CRITICAL_MPH = 22.0
PRECIP_WARNING_CHANCE = 0.50  # 0.0-1.0
COLD_WARNING_F = 25.0
HEAT_WARNING_F = 92.0

# A matchup split this far from league average is worth surfacing.
SPLIT_WARNING_DELTA = 3.0
SPLIT_CRITICAL_DELTA = 6.0
SPLIT_MIN_GAMES = 4           # ignore splits from too small a sample

# Value flags, in projected points per $1000 of salary.
VALUE_GOOD_PPK = 3.0
VALUE_POOR_PPK = 1.8

# --- Platform scoring (differ per platform) ---------------------------------
# DraftKings NFL Classic: full PPR, with yardage bonuses.
# FanDuel NFL: half PPR, no bonuses, harsher fumble penalty.

DK_SCORING = ScoringRules(
    platform=Platform.DRAFTKINGS,
    salary_cap=50_000,
    points_per_passing_yard=0.04,
    points_per_passing_td=4.0,
    points_per_interception=-1.0,
    points_per_rushing_yard=0.1,
    points_per_rushing_td=6.0,
    points_per_reception=1.0,
    points_per_receiving_yard=0.1,
    points_per_receiving_td=6.0,
    points_per_fumble_lost=-1.0,
    bonus_300_passing_yards=3.0,
    bonus_100_rushing_yards=3.0,
    bonus_100_receiving_yards=3.0,
)

FD_SCORING = ScoringRules(
    platform=Platform.FANDUEL,
    salary_cap=60_000,
    points_per_passing_yard=0.04,
    points_per_passing_td=4.0,
    points_per_interception=-1.0,
    points_per_rushing_yard=0.1,
    points_per_rushing_td=6.0,
    points_per_reception=0.5,
    points_per_receiving_yard=0.1,
    points_per_receiving_td=6.0,
    points_per_fumble_lost=-2.0,
)

SCORING = {
    Platform.DRAFTKINGS: DK_SCORING,
    Platform.FANDUEL: FD_SCORING,
}

# --- Lineup construction (differs per platform) -----------------------------
# Neither main-slate game includes a kicker; both run a nine-slot roster with a
# single RB/WR/TE flex. The caps differ, which is what makes the two optimal
# lineups diverge even from identical projections.

DK_LINEUP = LineupRules(
    platform=Platform.DRAFTKINGS,
    salary_cap=50_000,
    requirements={
        Position.QB: 1,
        Position.RB: 2,
        Position.WR: 3,
        Position.TE: 1,
        Position.DST: 1,
    },
    flex_count=1,
    flex_eligible=frozenset({Position.RB, Position.WR, Position.TE}),
)

FD_LINEUP = LineupRules(
    platform=Platform.FANDUEL,
    salary_cap=60_000,
    requirements={
        Position.QB: 1,
        Position.RB: 2,
        Position.WR: 3,
        Position.TE: 1,
        Position.DST: 1,
    },
    flex_count=1,
    flex_eligible=frozenset({Position.RB, Position.WR, Position.TE}),
)

LINEUPS = {
    Platform.DRAFTKINGS: DK_LINEUP,
    Platform.FANDUEL: FD_LINEUP,
}

SALARY_CAPS = {p.value: r.salary_cap for p, r in SCORING.items()}

# --- Stadiums ---------------------------------------------------------------
# `is_dome` covers retractable roofs too: when closed the forecast is moot, and
# treating them as indoor is the safer default for flagging.

STADIUMS: dict[str, Venue] = {
    "ARI": Venue("State Farm Stadium", 33.5276, -112.2626, is_dome=True),
    "ATL": Venue("Mercedes-Benz Stadium", 33.7554, -84.4008, is_dome=True),
    "BAL": Venue("M&T Bank Stadium", 39.2780, -76.6227),
    "BUF": Venue("Highmark Stadium", 42.7738, -78.7870),
    "CAR": Venue("Bank of America Stadium", 35.2258, -80.8528),
    "CHI": Venue("Soldier Field", 41.8623, -87.6167),
    "CIN": Venue("Paycor Stadium", 39.0955, -84.5161),
    "CLE": Venue("Huntington Bank Field", 41.5061, -81.6995),
    "DAL": Venue("AT&T Stadium", 32.7473, -97.0945, is_dome=True),
    "DEN": Venue("Empower Field at Mile High", 39.7439, -105.0201),
    "DET": Venue("Ford Field", 42.3400, -83.0456, is_dome=True),
    "GB": Venue("Lambeau Field", 44.5013, -88.0622),
    "HOU": Venue("NRG Stadium", 29.6847, -95.4107, is_dome=True),
    "IND": Venue("Lucas Oil Stadium", 39.7601, -86.1639, is_dome=True),
    "JAX": Venue("EverBank Stadium", 30.3239, -81.6373),
    "KC": Venue("Arrowhead Stadium", 39.0489, -94.4839),
    "LAC": Venue("SoFi Stadium", 33.9535, -118.3392, is_dome=True),
    "LAR": Venue("SoFi Stadium", 33.9535, -118.3392, is_dome=True),
    "LV": Venue("Allegiant Stadium", 36.0909, -115.1833, is_dome=True),
    "MIA": Venue("Hard Rock Stadium", 25.9580, -80.2389),
    "MIN": Venue("U.S. Bank Stadium", 44.9736, -93.2575, is_dome=True),
    "NE": Venue("Gillette Stadium", 42.0909, -71.2643),
    "NO": Venue("Caesars Superdome", 29.9511, -90.0812, is_dome=True),
    "NYG": Venue("MetLife Stadium", 40.8135, -74.0745),
    "NYJ": Venue("MetLife Stadium", 40.8135, -74.0745),
    "PHI": Venue("Lincoln Financial Field", 39.9008, -75.1675),
    "PIT": Venue("Acrisure Stadium", 40.4468, -80.0158),
    "SEA": Venue("Lumen Field", 47.5952, -122.3316),
    "SF": Venue("Levi's Stadium", 37.4033, -121.9694),
    "TB": Venue("Raymond James Stadium", 27.9759, -82.5033),
    "TEN": Venue("Nissan Stadium", 36.1665, -86.7713),
    "WAS": Venue("Northwest Stadium", 38.9076, -76.8645),
}


def venue_for(team: str) -> Venue | None:
    """Look up a home venue by team abbreviation."""
    return STADIUMS.get(team.upper())
