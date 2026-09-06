"""Matchup / defensive split ingestion, from nflverse weekly player stats.

nflverse publishes the nflfastR-derived weekly stats as one CSV per season, free
and without authentication. Each row is one player's week, and critically it
carries `opponent_team` — so summing fantasy points by opponent and position
gives fantasy points allowed by each defense.

A season file is a few megabytes, so it is downloaded once and cached on disk.
Aggregation happens in memory with the stdlib `csv` module; pandas would be
convenient but is a heavy dependency for one group-by.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import requests

import config
from models import MatchupSplit, Position

# Only these positions appear in the weekly player stats with meaningful
# fantasy scoring. K and DST are tracked separately upstream.
TRACKED_POSITIONS = {Position.QB, Position.RB, Position.WR, Position.TE}


class SplitsUnavailable(Exception):
    """Raised when split data cannot be retrieved."""


def _season_url(season: int) -> str:
    return f"{config.NFLVERSE_BASE_URL}/player_stats_{season}.csv"


def _cached_path(season: int) -> Path:
    return config.CACHE_DIR / f"player_stats_{season}.csv"


def _download_season(season: int) -> str:
    """Fetch a season CSV, using the on-disk cache when present."""
    cached = _cached_path(season)
    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_text(encoding="utf-8")

    try:
        resp = requests.get(
            _season_url(season), timeout=config.REQUEST_TIMEOUT_SECONDS * 4
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SplitsUnavailable(
            f"Could not download {season} player stats: {exc}"
        ) from exc

    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(resp.text, encoding="utf-8")
    return resp.text


@dataclass
class _Tally:
    points: float = 0.0
    weeks: set[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.weeks is None:
            self.weeks = set()


class SplitsTable:
    """Fantasy points allowed per defense per position, for one season.

    Load once and query many times — building it parses the whole season file,
    so constructing it per lookup would be wasteful.
    """

    def __init__(self, season: int, scoring: str = "ppr") -> None:
        self.season = season
        self.scoring = scoring
        self._per_game: dict[tuple[str, Position], float] = {}
        self._games: dict[tuple[str, Position], int] = {}
        self._league_average: dict[Position, float] = {}
        self._build()

    def _points_column(self) -> str:
        return "fantasy_points_ppr" if self.scoring == "ppr" else "fantasy_points"

    def _build(self) -> None:
        text = _download_season(self.season)
        column = self._points_column()
        tallies: dict[tuple[str, Position], _Tally] = defaultdict(_Tally)

        reader = csv.DictReader(io.StringIO(text))
        if column not in (reader.fieldnames or []):
            raise SplitsUnavailable(
                f"Column '{column}' missing from {self.season} stats file"
            )

        for row in reader:
            if row.get("season_type") != "REG":
                continue

            defense = (row.get("opponent_team") or "").strip().upper()
            if not defense:
                continue

            try:
                position = Position(row.get("position", "").strip())
            except ValueError:
                continue
            if position not in TRACKED_POSITIONS:
                continue

            try:
                points = float(row.get(column) or 0.0)
                week = int(row.get("week") or 0)
            except ValueError:
                continue

            tally = tallies[(defense, position)]
            tally.points += points
            tally.weeks.add(week)

        if not tallies:
            raise SplitsUnavailable(f"No usable rows in {self.season} stats file")

        for key, tally in tallies.items():
            games = len(tally.weeks)
            if games == 0:
                continue
            self._per_game[key] = tally.points / games
            self._games[key] = games

        by_position: dict[Position, list[float]] = defaultdict(list)
        for (_, position), per_game in self._per_game.items():
            by_position[position].append(per_game)
        self._league_average = {
            position: sum(values) / len(values)
            for position, values in by_position.items()
            if values
        }

    def split_for(self, opponent: str, position: Position) -> MatchupSplit | None:
        key = (opponent.strip().upper(), position)
        per_game = self._per_game.get(key)
        average = self._league_average.get(position)
        if per_game is None or average is None:
            return None

        return MatchupSplit(
            opponent=key[0],
            position=position,
            fantasy_points_allowed=per_game,
            league_average=average,
            games_sampled=self._games.get(key, 0),
        )

    def rankings(self, position: Position) -> list[tuple[str, float]]:
        """Defenses ordered most to least generous to `position`."""
        rows = [
            (team, per_game)
            for (team, pos), per_game in self._per_game.items()
            if pos == position
        ]
        return sorted(rows, key=lambda row: row[1], reverse=True)


_TABLE_CACHE: dict[tuple[int, str], SplitsTable] = {}


def get_table(season: int | None = None, scoring: str | None = None) -> SplitsTable:
    """Return a cached `SplitsTable`, building it on first use."""
    season = season if season is not None else config.SPLITS_SEASON
    scoring = scoring if scoring is not None else config.SPLITS_SCORING
    key = (season, scoring)
    if key not in _TABLE_CACHE:
        _TABLE_CACHE[key] = SplitsTable(season, scoring)
    return _TABLE_CACHE[key]


def fetch_split(
    opponent: str, position: Position, season: int | None = None
) -> MatchupSplit | None:
    """Return how `opponent` has defended `position`.

    Returns None when there is no data for that pairing, so a missing split
    degrades to "no flag" rather than breaking the run.
    """
    return get_table(season).split_for(opponent, position)
