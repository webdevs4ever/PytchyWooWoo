"""Shared internal data shapes.

Every source module normalizes its provider's response into these types, so the
rules engine never sees a provider-specific payload. When an upstream API changes
shape, the change is absorbed in `sources/`, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Position(str, Enum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DST = "DST"


class Platform(str, Enum):
    DRAFTKINGS = "draftkings"
    FANDUEL = "fanduel"


class Severity(str, Enum):
    """How strongly a flag should push a lineup decision."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Venue:
    name: str
    latitude: float
    longitude: float
    is_dome: bool = False


@dataclass(frozen=True)
class Game:
    game_id: str
    home_team: str
    away_team: str
    kickoff: datetime
    venue: Venue

    def __str__(self) -> str:
        return f"{self.away_team} @ {self.home_team}"


@dataclass(frozen=True)
class WeatherCondition:
    """Forecast at kickoff. Dome games short-circuit to `indoor()`."""

    temperature_f: float
    wind_mph: float
    precipitation_chance: float  # 0.0-1.0
    description: str
    is_indoor: bool = False

    @classmethod
    def indoor(cls) -> WeatherCondition:
        return cls(
            temperature_f=70.0,
            wind_mph=0.0,
            precipitation_chance=0.0,
            description="Indoor (dome)",
            is_indoor=True,
        )


@dataclass(frozen=True)
class Player:
    player_id: str
    name: str
    position: Position
    team: str

    def __str__(self) -> str:
        return f"{self.name} ({self.position.value}, {self.team})"


@dataclass(frozen=True)
class Pricing:
    """One platform's salary and projection for one player.

    DraftKings and FanDuel price and score players differently, so pricing is
    always per-platform rather than shared.
    """

    platform: Platform
    salary: int
    projected_points: float

    @property
    def points_per_thousand(self) -> float:
        """Standard value metric: projected points per $1000 of cap space."""
        if self.salary <= 0:
            return 0.0
        return self.projected_points / (self.salary / 1000)


@dataclass(frozen=True)
class MatchupSplit:
    """How generous an opponent has been to one position."""

    opponent: str
    position: Position
    fantasy_points_allowed: float
    league_average: float
    games_sampled: int

    @property
    def delta(self) -> float:
        """Positive means the opponent gives up more than league average."""
        return self.fantasy_points_allowed - self.league_average


@dataclass(frozen=True)
class Flag:
    code: str
    severity: Severity
    reason: str
    player: Player | None = None
    game: Game | None = None

    def __str__(self) -> str:
        subject = self.player.name if self.player else (str(self.game) if self.game else "-")
        return f"[{self.severity.value.upper():8}] {subject}: {self.reason}"


@dataclass
class PlayerSlate:
    """Everything known about one player in one game, assembled for the rules engine."""

    player: Player
    game: Game
    weather: WeatherCondition | None = None
    split: MatchupSplit | None = None
    pricing: dict[Platform, Pricing] = field(default_factory=dict)


@dataclass(frozen=True)
class StatLine:
    """Projected raw stats for one player-game.

    Deliberately platform-neutral: these are football outcomes, not points.
    Converting to points is `ScoringRules`' job, because DraftKings and FanDuel
    score the same stat line differently.
    """

    passing_yards: float = 0.0
    passing_tds: float = 0.0
    interceptions: float = 0.0
    rushing_yards: float = 0.0
    rushing_tds: float = 0.0
    receptions: float = 0.0
    receiving_yards: float = 0.0
    receiving_tds: float = 0.0
    fumbles_lost: float = 0.0

    def is_empty(self) -> bool:
        """True when no projection data was found — distinct from a real zero."""
        return all(
            getattr(self, f) == 0.0
            for f in (
                "passing_yards",
                "passing_tds",
                "rushing_yards",
                "rushing_tds",
                "receptions",
                "receiving_yards",
                "receiving_tds",
            )
        )


@dataclass(frozen=True)
class ScoringRules:
    """One platform's scoring system.

    DraftKings is full PPR with yardage bonuses; FanDuel is half PPR with none.
    Applying the wrong one silently misvalues every player, so pricing is always
    computed per platform rather than shared.
    """

    platform: Platform
    salary_cap: int
    points_per_passing_yard: float
    points_per_passing_td: float
    points_per_interception: float
    points_per_rushing_yard: float
    points_per_rushing_td: float
    points_per_reception: float
    points_per_receiving_yard: float
    points_per_receiving_td: float
    points_per_fumble_lost: float
    bonus_300_passing_yards: float = 0.0
    bonus_100_rushing_yards: float = 0.0
    bonus_100_receiving_yards: float = 0.0

    def score(self, stats: StatLine) -> float:
        total = (
            stats.passing_yards * self.points_per_passing_yard
            + stats.passing_tds * self.points_per_passing_td
            + stats.interceptions * self.points_per_interception
            + stats.rushing_yards * self.points_per_rushing_yard
            + stats.rushing_tds * self.points_per_rushing_td
            + stats.receptions * self.points_per_reception
            + stats.receiving_yards * self.points_per_receiving_yard
            + stats.receiving_tds * self.points_per_receiving_td
            + stats.fumbles_lost * self.points_per_fumble_lost
        )

        # Bonuses are all-or-nothing thresholds, so they apply to a projection
        # only when the projected total clears the line.
        if stats.passing_yards >= 300:
            total += self.bonus_300_passing_yards
        if stats.rushing_yards >= 100:
            total += self.bonus_100_rushing_yards
        if stats.receiving_yards >= 100:
            total += self.bonus_100_receiving_yards

        return round(total, 2)


@dataclass(frozen=True)
class InjuryStatus:
    """A player's standing on the official injury report.

    `report_status` is the game-status designation ("Out", "Doubtful",
    "Questionable") and is empty when a player appears on the practice report
    without a game designation.
    """

    player_name: str
    team: str
    week: int
    report_status: str = ""
    primary_injury: str = ""
    practice_status: str = ""

    @property
    def is_ruled_out(self) -> bool:
        return self.report_status.strip().lower() == "out"

    @property
    def is_doubtful(self) -> bool:
        return self.report_status.strip().lower() == "doubtful"

    @property
    def is_questionable(self) -> bool:
        return self.report_status.strip().lower() == "questionable"

    def __str__(self) -> str:
        detail = self.primary_injury or self.practice_status or "no detail"
        return f"{self.report_status or 'listed'} ({detail})"
