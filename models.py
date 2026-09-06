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
    number: int | None = None  # jersey number, shown on dashboard cards

    def __str__(self) -> str:
        return f"{self.name} ({self.position.value}, {self.team})"

    @property
    def initials(self) -> str:
        """Two-letter monogram for the card avatar, e.g. 'Josh Allen' -> 'JA'."""
        parts = [p for p in self.name.replace(".", " ").split() if p]
        if not parts:
            return "??"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


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
    # Set when a flag is true of one platform only, so a platform-scoped view
    # can filter it. Cross-platform and platform-neutral flags leave it None.
    platform: Platform | None = None

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
    # Colour, not signal — never consumed by the rules engine.
    narratives: list["Narrative"] = field(default_factory=list)


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


@dataclass(frozen=True)
class RosterEntry:
    """A player's roster record: identity, team, number, and standing."""

    player_name: str
    team: str
    position: str
    jersey_number: int | None = None
    status: str = ""

    @property
    def is_active(self) -> bool:
        """True for players on the active roster.

        nflverse uses 'ACT' for active; reserve, practice squad, and injured
        designations carry their own codes.
        """
        return self.status.strip().upper() in {"ACT", "ACTIVE", ""}


class NarrativeKind(str, Enum):
    REVENGE = "revenge"    # facing a former team
    REUNION = "reunion"    # facing former teammates


class NarrativeStrength(str, Enum):
    STRONG = "strong"
    WEAK = "weak"


@dataclass(frozen=True)
class Narrative:
    """A story about a matchup, not a signal about it.

    Kept deliberately separate from `Flag`. A flag says something that should
    change a lineup decision; a narrative says something that makes the game
    worth watching. Conflating them would put colour where signal belongs.
    """

    kind: NarrativeKind
    headline: str
    detail: str
    player: Player | None = None
    seasons: tuple[int, ...] = ()
    strength: NarrativeStrength = NarrativeStrength.WEAK
    # Why this one fired, and the rule that graded it. Printed in the admin
    # console so the judgement being applied is visible rather than implied.
    why: str = ""
    rule: str = ""

    @property
    def is_strong(self) -> bool:
        return self.strength is NarrativeStrength.STRONG

    def __str__(self) -> str:
        return f"[{self.strength.value}/{self.kind.value}] {self.headline} — {self.detail}"


@dataclass(frozen=True)
class LineupRules:
    """How a valid lineup is constructed on one platform.

    Separate from `ScoringRules` because these govern roster shape rather than
    points. Both change when a platform revises its game, so both are versioned
    together in `sources/manager.py`.
    """

    platform: Platform
    salary_cap: int
    requirements: dict[Position, int]      # fixed slots per position
    flex_count: int = 0
    flex_eligible: frozenset[Position] = frozenset()

    @property
    def total_slots(self) -> int:
        return sum(self.requirements.values()) + self.flex_count

    def slot_labels(self) -> list[str]:
        """Slot names in lineup order, e.g. QB, RB1, RB2, WR1..., FLEX, DST."""
        labels: list[str] = []
        for position in (Position.QB, Position.RB, Position.WR, Position.TE, Position.DST):
            count = self.requirements.get(position, 0)
            if count == 1:
                labels.append(position.value)
            else:
                labels.extend(f"{position.value}{i + 1}" for i in range(count))
        labels.extend(
            "FLEX" if self.flex_count == 1 else f"FLEX{i + 1}"
            for i in range(self.flex_count)
        )
        return labels


@dataclass(frozen=True)
class LineupEntry:
    """One filled slot."""

    slot: str
    player: Player
    salary: int
    projected_points: float


@dataclass
class Lineup:
    platform: Platform
    entries: list[LineupEntry] = field(default_factory=list)

    @property
    def salary(self) -> int:
        return sum(e.salary for e in self.entries)

    @property
    def projected_points(self) -> float:
        return round(sum(e.projected_points for e in self.entries), 2)

    def player_ids(self) -> set[str]:
        return {e.player.player_id for e in self.entries}
