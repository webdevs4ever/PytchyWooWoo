"""Prediction-market helper for Kalshi and Polymarket football questions.

Answers questions of the form "will this player exceed N of some stat" with:

1. **Historical analysis** over the seasons nflverse publishes.
2. **A percentage**, which is an empirical hit rate adjusted for known factors —
   injury status, matchup, weather. Read the section below before trusting it.
3. **A Narrative Street flag** where one applies.

## What the percentage is, and is not

It is the share of that player's past games in which the threshold was met,
shifted by conditions we can observe. It is a **base rate**, not a forecast. It
carries no information the market does not already have, and prediction-market
prices routinely incorporate far more — beat reporting, line movement, sharp
money.

Three things it cannot see: usage changes (a player promoted to WR1 this week),
game script (a blowout that ends the passing game), and anything reported since
the last data refresh. A wide interval on a small sample is the honest signal
that the number should not be leaned on, which is why the interval is always
reported alongside it.
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sources"

import csv
import io
import math
from dataclasses import dataclass, field

import config
from models import Narrative

# Stat columns in the nflverse weekly file, by the name a market question uses.
STATS = {
    "passing_yards": "passing_yards",
    "passing_tds": "passing_tds",
    "interceptions": "interceptions",
    "rushing_yards": "rushing_yards",
    "rushing_tds": "rushing_tds",
    "receiving_yards": "receiving_yards",
    "receiving_tds": "receiving_tds",
    "receptions": "receptions",
    "fantasy_points": "fantasy_points_ppr",
}

# Which stats degrade in wind or rain, for the weather adjustment.
WIND_SENSITIVE_STATS = {"passing_yards", "passing_tds", "receiving_yards", "receptions"}


class MarketsUnavailable(Exception):
    """Raised when historical data cannot be assembled."""


@dataclass(frozen=True)
class Question:
    """A market question. `over=False` asks whether the player stays under."""

    player_name: str
    stat: str
    threshold: float
    over: bool = True

    def __str__(self) -> str:
        direction = "over" if self.over else "under"
        return f"{self.player_name} {direction} {self.threshold} {self.stat.replace('_', ' ')}"


@dataclass
class Adjustment:
    label: str
    delta: float          # percentage points, signed
    reason: str


@dataclass
class Analysis:
    question: Question
    seasons: list[int] = field(default_factory=list)
    games: int = 0
    hits: int = 0
    values: list[float] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    narrative: Narrative | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def base_rate(self) -> float:
        """Share of past games meeting the threshold, as a percentage."""
        return 100.0 * self.hits / self.games if self.games else 0.0

    @property
    def estimate(self) -> float:
        """Base rate after adjustments, clamped to a sane range.

        Never returns 0 or 100: football does not offer certainties, and a
        confident extreme from a handful of games is the most misleading thing
        this module could print.
        """
        value = self.base_rate + sum(a.delta for a in self.adjustments)
        return max(2.0, min(98.0, value))

    @property
    def interval(self) -> tuple[float, float]:
        """Wilson 95% interval on the base rate.

        Reported always. A 67% from three games and a 67% from forty are
        different claims, and only the interval says so.
        """
        if not self.games:
            return (0.0, 100.0)
        z, n, p = 1.96, self.games, self.hits / self.games
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
        return (max(0.0, 100 * (centre - margin)), min(100.0, 100 * (centre + margin)))

    @property
    def confidence(self) -> str:
        low, high = self.interval
        width = high - low
        if self.games < 6:
            return "very low — too few games to say anything"
        if width > 40:
            return "low — the interval is wider than most edges"
        if width > 25:
            return "moderate"
        return "reasonable for a base rate"

    @property
    def average(self) -> float:
        return sum(self.values) / len(self.values) if self.values else 0.0


def _load_history(player_name: str, seasons: list[int]) -> tuple[list[dict], list[int]]:
    """Weekly regular-season rows for one player, newest season first."""
    from sources.odds import strict_name
    from sources.splits import SplitsUnavailable, _download_season

    key = strict_name(player_name)
    rows: list[dict] = []
    found: list[int] = []

    for season in seasons:
        try:
            text = _download_season(season)
        except SplitsUnavailable:
            continue
        matched = [
            row
            for row in csv.DictReader(io.StringIO(text))
            if row.get("season_type") == "REG"
            and strict_name(row.get("player_display_name", "")) == key
        ]
        if matched:
            rows.extend(matched)
            found.append(season)

    return rows, found


def analyze(
    question: Question,
    seasons: list[int] | None = None,
    slate=None,
) -> Analysis:
    """Answer a market question from history, conditions, and narrative.

    `slate` is an optional `PlayerSlate`; when given, its weather, matchup
    split, and injury status feed the adjustments.
    """
    column = STATS.get(question.stat)
    if column is None:
        raise MarketsUnavailable(
            f"Unknown stat {question.stat!r}. Known: {', '.join(sorted(STATS))}"
        )

    seasons = seasons or [
        config.SPLITS_SEASON - offset for offset in range(config.MARKET_LOOKBACK)
    ]
    rows, found = _load_history(question.player_name, seasons)

    analysis = Analysis(question=question, seasons=found)
    if not rows:
        analysis.notes.append(
            f"No regular-season games found for {question.player_name} in "
            f"{', '.join(str(s) for s in seasons)}."
        )
        return analysis

    for row in rows:
        try:
            value = float(row.get(column) or 0.0)
        except ValueError:
            continue
        analysis.values.append(value)
        hit = value > question.threshold if question.over else value < question.threshold
        analysis.hits += int(hit)
        analysis.games += 1

    if len(found) < config.MARKET_LOOKBACK:
        missing = sorted(set(seasons) - set(found))
        analysis.notes.append(
            f"Only {len(found)} of the requested {config.MARKET_LOOKBACK} seasons "
            f"are published; {', '.join(str(s) for s in missing)} unavailable."
        )

    _apply_adjustments(analysis, slate)
    return analysis


def _apply_adjustments(analysis: Analysis, slate) -> None:
    """Shift the base rate for conditions we can actually observe."""
    question = analysis.question

    # --- injury ---
    try:
        from sources.injuries import InjuriesUnavailable, fetch_status

        status = fetch_status(question.player_name)
    except Exception:
        status = None

    if status is None:
        try:
            from sources.injuries import get_report

            report = get_report()
            if report.is_sparse:
                analysis.notes.append(
                    f"No injury adjustment applied: the {report.season} report has "
                    f"only {len(report)} rows filed. Absence of a designation here "
                    "is not evidence the player is healthy."
                )
        except Exception:
            pass

    if status is not None:
        if status.is_ruled_out:
            analysis.adjustments.append(
                Adjustment(
                    "ruled out",
                    -95.0 if question.over else 95.0,
                    f"listed Out ({status.primary_injury or 'no detail'}) — "
                    "will not play, so an over cannot hit",
                )
            )
        elif status.is_doubtful:
            analysis.adjustments.append(
                Adjustment("doubtful", -25.0 if question.over else 25.0,
                           f"listed Doubtful ({status.primary_injury or 'no detail'})")
            )
        elif status.is_questionable:
            analysis.adjustments.append(
                Adjustment("questionable", -7.0 if question.over else 7.0,
                           f"listed Questionable ({status.primary_injury or 'no detail'})")
            )

    if slate is None:
        return

    # --- matchup ---
    split = getattr(slate, "split", None)
    if split is not None and split.games_sampled >= config.SPLIT_MIN_GAMES:
        delta = split.delta
        if abs(delta) >= config.SPLIT_WARNING_DELTA:
            shift = max(-12.0, min(12.0, delta * 1.5))
            analysis.adjustments.append(
                Adjustment(
                    "matchup",
                    shift if question.over else -shift,
                    f"{split.opponent} allows {split.fantasy_points_allowed:.1f} FP to "
                    f"{split.position.value}s, {abs(delta):.1f} "
                    f"{'above' if delta > 0 else 'below'} average",
                )
            )

    # --- weather ---
    weather = getattr(slate, "weather", None)
    if weather is not None and not weather.is_indoor:
        if question.stat in WIND_SENSITIVE_STATS:
            if weather.wind_mph >= config.WIND_CRITICAL_MPH:
                analysis.adjustments.append(
                    Adjustment("wind", -12.0 if question.over else 12.0,
                               f"{weather.wind_mph:.0f} mph wind at kickoff")
                )
            elif weather.wind_mph >= config.WIND_WARNING_MPH:
                analysis.adjustments.append(
                    Adjustment("wind", -6.0 if question.over else 6.0,
                               f"{weather.wind_mph:.0f} mph wind at kickoff")
                )
        if weather.precipitation_chance >= config.PRECIP_WARNING_CHANCE:
            analysis.adjustments.append(
                Adjustment("precipitation", -4.0 if question.over else 4.0,
                           f"{weather.precipitation_chance:.0%} chance of precipitation")
            )

    # --- narrative ---
    try:
        from sources.narratives import for_slate, get_history

        stories = for_slate(slate, get_history())
        if stories:
            analysis.narrative = stories[0]
    except Exception:
        pass


# --- Output -----------------------------------------------------------------


def format_analysis(analysis: Analysis) -> str:
    q = analysis.question
    lines = [f"{q}", "=" * min(72, max(40, len(str(q))))]

    if not analysis.games:
        lines.extend(f"  {note}" for note in analysis.notes)
        return "\n".join(lines)

    low, high = analysis.interval
    lines += [
        "",
        f"  History     {analysis.hits} of {analysis.games} games "
        f"({', '.join(str(s) for s in analysis.seasons)})",
        f"  Average     {analysis.average:.1f} "
        f"{q.stat.replace('_', ' ')} per game",
        f"  Base rate   {analysis.base_rate:.1f}%   "
        f"95% interval {low:.0f}–{high:.0f}%",
    ]

    if analysis.adjustments:
        lines += ["", "  Adjustments"]
        for adjustment in analysis.adjustments:
            lines.append(
                f"    {adjustment.delta:+6.1f}  {adjustment.label:14} {adjustment.reason}"
            )

    lines += [
        "",
        f"  ESTIMATE    {analysis.estimate:.0f}%",
        f"  Confidence  {analysis.confidence}",
    ]

    if analysis.narrative:
        n = analysis.narrative
        lines += [
            "",
            f"  NARRATIVE STREET  [{n.strength.value}] {n.headline}",
            f"                    {n.detail}",
        ]

    if analysis.notes:
        lines.append("")
        lines.extend(f"  Note: {note}" for note in analysis.notes)

    lines += [
        "",
        "  This is a base rate adjusted for observable conditions, not a",
        "  forecast. It carries no information the market lacks, and cannot see",
        "  usage changes, game script, or anything reported since the last",
        "  data refresh.",
    ]
    return "\n".join(lines)


def slate_for(player_name: str):
    """Find this week's slate entry for a player, so conditions can be applied.

    Returns None when the player is not playing this week, or the schedule and
    rosters cannot be reached.
    """
    from models import Player, PlayerSlate, Position
    from sources.narratives import get_history
    from sources.schedule import current_week, games_for

    try:
        history = get_history()
        season, week = current_week()
        games = games_for(season, week)
    except Exception:
        return None

    from sources.odds import strict_name

    key = strict_name(player_name)
    for game in games:
        for team in (game.home_team, game.away_team):
            if key not in history.roster_now(team):
                continue
            try:
                position = Position(history.position(key))
            except ValueError:
                return None
            slate = PlayerSlate(
                player=Player(key, history.display_name(key), position, team),
                game=game,
            )
            try:
                import main as pipeline

                pipeline.enrich_with_weather([slate], quiet=True)
                pipeline.enrich_with_splits([slate], quiet=True)
            except Exception:
                pass
            return slate
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Analyze a Kalshi/Polymarket football question."
    )
    parser.add_argument("player", help='player name, e.g. "Amon-Ra St. Brown"')
    parser.add_argument("stat", choices=sorted(STATS), help="stat the market is on")
    parser.add_argument("threshold", type=float, help="the line")
    parser.add_argument(
        "--under", action="store_true", help="ask whether it stays under the line"
    )
    parser.add_argument(
        "--no-conditions",
        action="store_true",
        help="history only — skip weather, matchup, injury, and narrative",
    )
    parser.add_argument("--seasons", type=int, help="how many seasons to look back")
    args = parser.parse_args(argv)

    question = Question(args.player, args.stat, args.threshold, over=not args.under)
    slate = None if args.no_conditions else slate_for(args.player)

    seasons = None
    if args.seasons:
        seasons = [config.SPLITS_SEASON - offset for offset in range(args.seasons)]

    try:
        analysis = analyze(question, seasons=seasons, slate=slate)
    except MarketsUnavailable as exc:
        print(exc)
        return 1

    print(format_analysis(analysis))
    return 0 if analysis.games else 1


if __name__ == "__main__":
    raise SystemExit(main())
