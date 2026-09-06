"""Narrative Street — stories in a matchup, derived from roster history.

Two kinds, both falling out of the same data:

- **Revenge game.** A player faces a team they were on in a prior season.
- **Reunion.** A player faces people who were their teammates in a prior season.

No new provider is needed. nflverse publishes one roster file per season, so
diffing seasons yields every team change — 681 players moved between 2025 and
2026 alone — and the schedule supplies who is playing whom.

Narratives are colour, not signal: they never produce a `Flag`, and nothing in
the rules engine consumes them.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

import config
from models import (
    Narrative,
    NarrativeKind,
    NarrativeStrength,
    Player,
    PlayerSlate,
    Position,
)
from sources.odds import strict_name
from sources.rosters import RostersUnavailable, _download_season, resolve_season

# Names that reduce to the same key across seasons are the join. Reuse the
# strict form — the loose one collides badly, as the roster lookup found.

# Only these positions make a reunion legible. A shared roster spot with a
# practice-squad lineman is churn; one with a quarterback or receiver is a story.
SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "K"}


class NarrativesUnavailable(Exception):
    """Raised when roster history cannot be assembled."""


@dataclass(frozen=True)
class Stint:
    season: int
    team: str


class RosterHistory:
    """Several seasons of rosters, indexed for team-change and teammate queries."""

    def __init__(self, seasons: list[int] | None = None) -> None:
        self.seasons = sorted(seasons or self._default_seasons(), reverse=True)
        # player key -> season -> team
        self._stints: dict[str, dict[int, str]] = {}
        # season -> team -> set of player keys
        self._squads: dict[int, dict[str, set[str]]] = {}
        # player key -> display name
        self._names: dict[str, str] = {}
        # player key -> position, from their most recent stint
        self._positions: dict[str, str] = {}
        self._build()

    @staticmethod
    def _default_seasons() -> list[int]:
        current = resolve_season()
        return [current - offset for offset in range(config.NARRATIVE_LOOKBACK)]

    def _build(self) -> None:
        loaded = 0
        for season in self.seasons:
            try:
                text = _download_season(season)
            except RostersUnavailable:
                continue  # a season with no published file is simply skipped

            for row in csv.DictReader(io.StringIO(text)):
                name = row.get("full_name", "")
                key = strict_name(name)
                team = (row.get("team") or "").strip().upper()
                if not key or not team:
                    continue

                self._names.setdefault(key, name)
                self._positions.setdefault(key, (row.get("position") or "").strip().upper())
                self._stints.setdefault(key, {})[season] = team
                self._squads.setdefault(season, {}).setdefault(team, set()).add(key)
            loaded += 1

        if not loaded:
            raise NarrativesUnavailable(
                f"No roster files available for seasons {self.seasons}"
            )

    @property
    def current_season(self) -> int:
        return self.seasons[0]

    def former_teams(self, player_name: str, exclude: str) -> list[Stint]:
        """Prior-season teams for a player, excluding their current one."""
        stints = self._stints.get(strict_name(player_name), {})
        return [
            Stint(season, team)
            for season, team in sorted(stints.items(), reverse=True)
            if season != self.current_season and team != exclude.upper()
        ]

    def team_in(self, player_name: str, season: int) -> str | None:
        """Which team a player was on in a given season, if any."""
        return self._stints.get(strict_name(player_name), {}).get(season)

    def tenure(self, player_name: str, team: str) -> int:
        """How many tracked seasons a player spent on `team`.

        Feeds the revenge grading: a single season is a rental, not a history.
        """
        stints = self._stints.get(strict_name(player_name), {})
        return sum(1 for t in stints.values() if t.upper() == team.upper())

    def teammates(self, player_name: str, season: int, team: str) -> set[str]:
        """Everyone on `team` in `season`, excluding the player themselves."""
        squad = set(self._squads.get(season, {}).get(team.upper(), set()))
        squad.discard(strict_name(player_name))
        return squad

    def roster_now(self, team: str) -> set[str]:
        return set(self._squads.get(self.current_season, {}).get(team.upper(), set()))

    def display_name(self, key: str) -> str:
        return self._names.get(key, key)

    def position(self, key: str) -> str:
        return self._positions.get(key, "")

    def skill_players(self, keys: set[str]) -> set[str]:
        """Narrow a set of players to fantasy-relevant positions.

        Without this, reunions measure practice-squad churn rather than
        anything a reader would recognise as a story.
        """
        return {k for k in keys if self._positions.get(k, "") in SKILL_POSITIONS}

    def __len__(self) -> int:
        return len(self._stints)


# --- Detectors --------------------------------------------------------------


def find_revenge(slate: PlayerSlate, history: RosterHistory) -> Narrative | None:
    """A player facing a team they used to play for."""
    game = slate.game
    opponent = game.home_team if slate.player.team == game.away_team else game.away_team

    for stint in history.former_teams(slate.player.name, exclude=slate.player.team):
        if stint.team == opponent.upper():
            gap = history.current_season - stint.season
            when = "last season" if gap == 1 else f"{gap} seasons ago"
            tenure = history.tenure(slate.player.name, stint.team)

            strong = (
                gap <= config.REVENGE_STRONG_MAX_SEASONS_SINCE
                and tenure >= config.REVENGE_STRONG_MIN_TENURE
            )
            plural = "s" if tenure != 1 else ""

            return Narrative(
                kind=NarrativeKind.REVENGE,
                headline=f"Faces former team {stint.team}",
                detail=f"Was on {stint.team} {when} ({stint.season}), "
                f"now with {slate.player.team}.",
                player=slate.player,
                seasons=(stint.season,),
                strength=(
                    NarrativeStrength.STRONG if strong else NarrativeStrength.WEAK
                ),
                why=f"left {stint.team} after {tenure} season{plural}; "
                f"faces them {gap} season{'s' if gap != 1 else ''} later",
                rule=f"STRONG when the move was within "
                f"{config.REVENGE_STRONG_MAX_SEASONS_SINCE} season(s) and tenure "
                f">= {config.REVENGE_STRONG_MIN_TENURE} seasons",
            )
    return None


def find_reunion(
    slate: PlayerSlate, history: RosterHistory, minimum: int | None = None
) -> Narrative | None:
    """A player facing former teammates who have since scattered onto the opponent.

    Deliberately excludes the case where the player's former team *is* the
    opponent. That is a revenge game, already reported, and counting it here
    yields a tautology: play for a team last season, face them this season, and
    of course forty of their players were your teammates. The story is the
    opposite — people you played alongside somewhere else, now lined up against
    you.

    Only skill-position teammates count. Without that filter the detector
    measures practice-squad churn: a sweep of the 2026 roster returned groups of
    seven, made up entirely of players no reader would recognise.
    """
    minimum = config.REUNION_MIN if minimum is None else minimum
    game = slate.game
    opponent = (
        game.home_team if slate.player.team == game.away_team else game.away_team
    ).upper()
    opposing = history.roster_now(opponent)
    if not opposing:
        return None

    best: tuple[int, str, set[str]] | None = None

    for season in history.seasons[1:]:
        team_then = history.team_in(slate.player.name, season)
        # No stint that season, or the stint was with the opponent — the latter
        # is the revenge case, not a reunion.
        if not team_then or team_then.upper() == opponent:
            continue

        shared = history.skill_players(
            history.teammates(slate.player.name, season, team_then) & opposing
        )
        if best is None or len(shared) > len(best[2]):
            best = (season, team_then, shared)

    if best is None or len(best[2]) < minimum:
        return None

    season, team_then, shared = best
    sample = sorted(history.display_name(k) for k in shared)[:3]
    trailing = f", and {len(shared) - len(sample)} more" if len(shared) > len(sample) else ""

    count = len(shared)
    return Narrative(
        kind=NarrativeKind.REUNION,
        headline=f"{count} former {team_then} teammate"
        f"{'s' if count != 1 else ''} now on {opponent}",
        detail=f"Lined up with {', '.join(sample)}{trailing} on {team_then} in {season}.",
        player=slate.player,
        seasons=(season,),
        strength=(
            NarrativeStrength.STRONG
            if count >= config.REUNION_STRONG_MIN
            else NarrativeStrength.WEAK
        ),
        why=f"{count} skill-position teammates from {team_then} ({season}) "
        f"now on {opponent}",
        rule=f"STRONG needs {config.REUNION_STRONG_MIN}+ skill-position teammates",
    )


DETECTORS = [find_revenge, find_reunion]


def for_slate(slate: PlayerSlate, history: RosterHistory) -> list[Narrative]:
    """Every story about one player's matchup."""
    found = [detector(slate, history) for detector in DETECTORS]
    return [n for n in found if n is not None]


def scan_week(
    season: int | None = None, week: int | None = None, history: RosterHistory | None = None
) -> list[tuple[Player, Narrative]]:
    """Every narrative across every game in a week.

    This is what "across all rosters" requires: detection over the real
    schedule rather than a hand-built slate. Only skill-position players are
    scanned — a story about a backup guard is not one anybody will feature.
    """
    from sources.schedule import ScheduleUnavailable, current_week, games_for

    history = history or get_history()

    try:
        if season is None or week is None:
            season, week = current_week()
        games = games_for(season, week)
    except ScheduleUnavailable as exc:
        raise NarrativesUnavailable(f"Schedule unavailable: {exc}") from exc

    found: list[tuple[Player, Narrative]] = []

    for game in games:
        for team in (game.home_team, game.away_team):
            for key in history.roster_now(team):
                position = history.position(key)
                if position not in SKILL_POSITIONS:
                    continue
                try:
                    player = Player(key, history.display_name(key), Position(position), team)
                except ValueError:
                    continue

                slate = PlayerSlate(player=player, game=game)
                found.extend((player, n) for n in for_slate(slate, history))

    # Strong before weak, then revenge before reunion.
    found.sort(key=lambda pair: (not pair[1].is_strong, pair[1].kind.value,
                                 pair[0].name))
    return found


_HISTORY: RosterHistory | None = None


def get_history(seasons: list[int] | None = None) -> RosterHistory:
    global _HISTORY
    if _HISTORY is None:
        _HISTORY = RosterHistory(seasons)
    return _HISTORY
