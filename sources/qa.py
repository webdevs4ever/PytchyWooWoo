"""Data-quality validation.

This is the QA layer described in ARCHITECTURE.md, implemented as a plain module
rather than a scheduled agent or CI job. Those can wrap it later; the checks
belong in the repo either way.

The distinction from `rules.py` matters. A `Flag` is a *betting signal* — a real
condition the user should weigh. An `Issue` is a *defect* — missing, stale, or
incoherent data that would render wrong, break a UI, or silently suppress a flag
the user expected to see. The rules engine deliberately stays quiet when data is
absent; this module is what makes that silence visible.

Checks return `Issue`s rather than raising, so one bad player never aborts the
validation of a slate.
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    # Allow `python sources/qa.py` as well as `python -m sources.qa`:
    # running a file inside a package leaves the repo root off sys.path.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sources"


from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import config
from models import Platform, PlayerSlate, Position

# Positions genuinely absent from the weekly stats file. A missing split for
# these is expected; for anyone else it means a lookup failed.
POSITIONS_WITHOUT_SPLITS = {Position.K, Position.DST}

# NWS publishes roughly 156 hours ahead.
NWS_FORECAST_HORIZON_HOURS = 156


class IssueLevel(str, Enum):
    ERROR = "error"      # would render wrong or break — do not ship this row
    WARNING = "warning"  # degraded output the user should know about
    NOTICE = "notice"    # expected absence, recorded for transparency


@dataclass(frozen=True)
class Issue:
    level: IssueLevel
    code: str
    message: str
    subject: str = "-"

    def __str__(self) -> str:
        return f"[{self.level.value.upper():7}] {self.subject:24} {self.code:26} {self.message}"


# --- Integrity --------------------------------------------------------------


def check_slate_integrity(slate: PlayerSlate) -> list[Issue]:
    """Catch slates that are internally contradictory."""
    issues: list[Issue] = []
    player, game = slate.player, slate.game
    subject = player.name

    if player.team not in (game.home_team, game.away_team):
        issues.append(
            Issue(
                IssueLevel.ERROR,
                "integrity.team_mismatch",
                f"{player.team} plays in neither side of {game} — "
                "opponent lookups will be wrong",
                subject,
            )
        )

    kickoff = game.kickoff
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    hours_out = (kickoff - datetime.now(timezone.utc)).total_seconds() / 3600

    if hours_out < 0:
        issues.append(
            Issue(
                IssueLevel.WARNING,
                "integrity.kickoff_past",
                f"kickoff was {abs(hours_out):.0f}h ago — stale slate",
                subject,
            )
        )
    elif hours_out > NWS_FORECAST_HORIZON_HOURS:
        issues.append(
            Issue(
                IssueLevel.NOTICE,
                "integrity.beyond_forecast",
                f"kickoff is {hours_out:.0f}h out, past the "
                f"{NWS_FORECAST_HORIZON_HOURS}h NWS horizon — forecast will be approximate",
                subject,
            )
        )

    return issues


def check_duplicates(slates: list[PlayerSlate]) -> list[Issue]:
    """One player appearing twice double-counts them in any lineup built downstream."""
    seen: dict[tuple[str, str], int] = {}
    names: dict[tuple[str, str], str] = {}
    for slate in slates:
        key = (slate.player.player_id, slate.game.game_id)
        seen[key] = seen.get(key, 0) + 1
        names[key] = slate.player.name

    return [
        Issue(
            IssueLevel.ERROR,
            "integrity.duplicate_player",
            f"appears {count}x in the same game — double-counted in any lineup",
            names[key],
        )
        for key, count in seen.items()
        if count > 1
    ]


# --- Completeness -----------------------------------------------------------


def check_weather_present(slate: PlayerSlate) -> list[Issue]:
    if slate.weather is not None:
        return []
    return [
        Issue(
            IssueLevel.WARNING,
            "missing.weather",
            f"no forecast for {slate.game} — weather flags silently skipped",
            slate.player.name,
        )
    ]


def check_split_present(slate: PlayerSlate) -> list[Issue]:
    """A missing split is expected for kickers, a real gap for everyone else."""
    position = slate.player.position

    if slate.split is None:
        if position in POSITIONS_WITHOUT_SPLITS:
            return [
                Issue(
                    IssueLevel.NOTICE,
                    "missing.split_expected",
                    f"{position.value} has no split data upstream — matchup flags skipped",
                    slate.player.name,
                )
            ]
        return [
            Issue(
                IssueLevel.WARNING,
                "missing.split",
                f"no split for {position.value} — lookup failed or opponent unknown",
                slate.player.name,
            )
        ]

    if slate.split.games_sampled < config.SPLIT_MIN_GAMES:
        return [
            Issue(
                IssueLevel.NOTICE,
                "missing.split_sample",
                f"only {slate.split.games_sampled} games sampled "
                f"(minimum {config.SPLIT_MIN_GAMES}) — matchup flags suppressed",
                slate.player.name,
            )
        ]

    return []


def check_pricing_present(slate: PlayerSlate) -> list[Issue]:
    """Comps Mode needs both platforms; one alone cannot be compared."""
    if not slate.pricing:
        return [
            Issue(
                IssueLevel.WARNING,
                "missing.pricing",
                "no salary on either platform — value flags skipped",
                slate.player.name,
            )
        ]

    missing = {p.value for p in Platform} - {p.value for p in slate.pricing}
    if missing:
        return [
            Issue(
                IssueLevel.NOTICE,
                "missing.pricing_platform",
                f"no salary on {', '.join(sorted(missing))} — "
                "cross-platform comparison unavailable",
                slate.player.name,
            )
        ]

    return []


# --- Soundness --------------------------------------------------------------


def check_pricing_sane(slate: PlayerSlate) -> list[Issue]:
    """Catch pricing that is present but incoherent.

    A zero projection against a real salary is the dangerous case: it is not
    missing data, so the value check runs and confidently reports the worst
    value on the board.
    """
    issues: list[Issue] = []

    for platform, pricing in slate.pricing.items():
        label = platform.value

        if pricing.salary <= 0:
            issues.append(
                Issue(
                    IssueLevel.ERROR,
                    "unsound.salary_zero",
                    f"{label}: salary is {pricing.salary} — value math divides by it",
                    slate.player.name,
                )
            )
            continue

        cap = config.SCORING[platform].salary_cap
        if pricing.salary > cap:
            issues.append(
                Issue(
                    IssueLevel.ERROR,
                    "unsound.salary_over_cap",
                    f"{label}: ${pricing.salary:,} exceeds the ${cap:,} cap",
                    slate.player.name,
                )
            )

        if pricing.projected_points == 0:
            issues.append(
                Issue(
                    IssueLevel.ERROR,
                    "unsound.projection_zero",
                    f"{label}: ${pricing.salary:,} with a 0.0 projection — "
                    "will be flagged as the worst value on the board",
                    slate.player.name,
                )
            )
        elif pricing.projected_points < 0:
            issues.append(
                Issue(
                    IssueLevel.WARNING,
                    "unsound.projection_negative",
                    f"{label}: projection is {pricing.projected_points}",
                    slate.player.name,
                )
            )

    return issues


def check_weather_sane(slate: PlayerSlate) -> list[Issue]:
    """Forecast values outside physical plausibility mean a parse went wrong."""
    weather = slate.weather
    if weather is None:
        return []

    issues: list[Issue] = []
    if not -40 <= weather.temperature_f <= 130:
        issues.append(
            Issue(
                IssueLevel.ERROR,
                "unsound.temperature",
                f"{weather.temperature_f}°F is out of range — unit conversion likely failed",
                slate.player.name,
            )
        )
    if not 0 <= weather.wind_mph <= 100:
        issues.append(
            Issue(
                IssueLevel.ERROR,
                "unsound.wind",
                f"{weather.wind_mph} mph is out of range — wind string parse likely failed",
                slate.player.name,
            )
        )
    if not 0.0 <= weather.precipitation_chance <= 1.0:
        issues.append(
            Issue(
                IssueLevel.ERROR,
                "unsound.precipitation",
                f"{weather.precipitation_chance} is not a 0-1 probability",
                slate.player.name,
            )
        )
    return issues


# --- Availability -----------------------------------------------------------


def check_injury(slate: PlayerSlate) -> list[Issue]:
    """Flag players who will not play, or may not.

    Import is local because this is the only check needing network access; the
    rest run offline.
    """
    try:
        from sources.injuries import InjuriesUnavailable, fetch_status
    except ImportError:  # pragma: no cover
        return []

    try:
        status = fetch_status(slate.player.name)
    except InjuriesUnavailable as exc:
        return [
            Issue(
                IssueLevel.WARNING,
                "injury.unavailable",
                f"injury report could not be loaded: {exc}",
                slate.player.name,
            )
        ]

    if status is None:
        return []

    if status.is_ruled_out:
        return [
            Issue(
                IssueLevel.ERROR,
                "injury.out",
                f"ruled OUT ({status.primary_injury or 'no detail'}) "
                f"as of week {status.week} — will score zero",
                slate.player.name,
            )
        ]
    if status.is_doubtful:
        return [
            Issue(
                IssueLevel.WARNING,
                "injury.doubtful",
                f"doubtful ({status.primary_injury or 'no detail'}), week {status.week}",
                slate.player.name,
            )
        ]
    if status.is_questionable:
        return [
            Issue(
                IssueLevel.NOTICE,
                "injury.questionable",
                f"questionable ({status.primary_injury or 'no detail'}), week {status.week}",
                slate.player.name,
            )
        ]
    return []


def check_roster_status(slate: PlayerSlate) -> list[Issue]:
    """Confirm the player is on a current roster, with the team we think.

    A player who was cut, traded, or is on the practice squad will still price
    and score in the salary export, so this is the check that catches a lineup
    slot that cannot produce.
    """
    try:
        from sources.rosters import RostersUnavailable, fetch_entry
    except ImportError:  # pragma: no cover
        return []

    try:
        entry = fetch_entry(slate.player.name, slate.player.team)
    except RostersUnavailable as exc:
        return [
            Issue(
                IssueLevel.WARNING,
                "roster.unavailable",
                f"roster could not be loaded: {exc}",
                slate.player.name,
            )
        ]

    if entry is None:
        return [
            Issue(
                IssueLevel.WARNING,
                "roster.not_found",
                "not on any current roster, or the name is ambiguous — "
                "may be retired, released, or misspelled",
                slate.player.name,
            )
        ]

    issues: list[Issue] = []

    if entry.team != slate.player.team:
        issues.append(
            Issue(
                IssueLevel.WARNING,
                "roster.team_mismatch",
                f"listed on {entry.team}, slate says {slate.player.team} — "
                "traded or stale data",
                slate.player.name,
            )
        )

    if not entry.is_active:
        issues.append(
            Issue(
                IssueLevel.WARNING,
                "roster.inactive",
                f"roster status is {entry.status!r}, not active — may not play",
                slate.player.name,
            )
        )

    if entry.jersey_number is None:
        issues.append(
            Issue(
                IssueLevel.NOTICE,
                "roster.no_number",
                "no jersey number on file — card will fall back to position",
                slate.player.name,
            )
        )

    return issues


def check_active_that_week(slate: PlayerSlate) -> list[Issue]:
    """Was this player on the active roster for the week actually being played?

    The season roster says who is on a team *now*. A player can be on it and
    still have been on reserve, the practice squad, or cut in the week under
    consideration — and would score nothing. Checking against the season roster
    alone silently passes those.
    """
    game = slate.game
    if game.season is None or game.week is None:
        return [
            Issue(
                IssueLevel.NOTICE,
                "week.unknown",
                "game carries no season/week, so roster and injury status "
                "cannot be aligned to the week being played",
                slate.player.name,
            )
        ]

    try:
        from sources.rosters import RostersUnavailable, get_weekly

        weekly = get_weekly(game.season)
    except Exception as exc:
        return [
            Issue(
                IssueLevel.NOTICE,
                "week.rosters_unavailable",
                f"weekly rosters unavailable: {exc}",
                slate.player.name,
            )
        ]

    if game.week not in weekly.weeks:
        return [
            Issue(
                IssueLevel.NOTICE,
                "week.not_published",
                f"week {game.week} rosters are not published yet for "
                f"{game.season}; falling back to season roster",
                slate.player.name,
            )
        ]

    status = weekly.status_for(slate.player.name, game.week)
    if status is None:
        return [
            Issue(
                IssueLevel.WARNING,
                "week.not_rostered",
                f"not on any {game.season} week {game.week} roster — "
                "cannot produce",
                slate.player.name,
            )
        ]

    issues: list[Issue] = []
    if status != "ACT":
        issues.append(
            Issue(
                IssueLevel.WARNING,
                "week.not_active",
                f"status {status!r} in week {game.week}, not ACT — "
                "reserve, practice squad, or released",
                slate.player.name,
            )
        )

    team = weekly.team_for(slate.player.name, game.week)
    if team and team != slate.player.team:
        issues.append(
            Issue(
                IssueLevel.WARNING,
                "week.wrong_team",
                f"on {team} in week {game.week}, slate says "
                f"{slate.player.team}",
                slate.player.name,
            )
        )

    return issues


def check_injury_week(slate: PlayerSlate) -> list[Issue]:
    """Is the injury designation from the week being played?

    `fetch_status` returns a player's most recent report when no week is given.
    That is the wrong answer for any week but the latest: a designation carried
    over from week 18 says nothing about week 1.
    """
    game = slate.game
    if game.week is None:
        return []

    try:
        from sources.injuries import InjuriesUnavailable, get_report

        report = get_report()
    except Exception:
        return []

    exact = report.status_for(slate.player.name, week=game.week)
    latest = report.status_for(slate.player.name)

    if latest is None:
        return []

    if exact is None:
        return [
            Issue(
                IssueLevel.WARNING,
                "injury.wrong_week",
                f"no injury report for week {game.week}; the designation in use "
                f"is from week {latest.week} ({latest.report_status or 'listed'}) "
                "and may not apply",
                slate.player.name,
            )
        ]

    if exact.report_status != latest.report_status:
        return [
            Issue(
                IssueLevel.NOTICE,
                "injury.week_differs",
                f"week {game.week} says {exact.report_status or 'no designation'}, "
                f"latest report (week {latest.week}) says "
                f"{latest.report_status or 'no designation'}",
                slate.player.name,
            )
        ]

    return []


# --- Weather scenario coverage ----------------------------------------------

# Each case: label, condition, position, and the flag codes it must produce.
# Written as data so a threshold change in config shows up here as a failure
# rather than passing quietly.
def _weather_scenarios() -> list[tuple[str, object, object, set[str]]]:
    from models import Position as P
    from models import WeatherCondition as W

    warn_wind = config.WIND_WARNING_MPH
    crit_wind = config.WIND_CRITICAL_MPH

    return [
        ("dome", W.indoor(), P.K, set()),
        ("calm and mild", W(68, 3, 0.0, "Clear"), P.QB, set()),
        (
            "wind at the warning threshold",
            W(60, warn_wind, 0.0, "Windy"),
            P.K,
            {"weather.wind"},
        ),
        (
            "wind below the threshold",
            W(60, warn_wind - 1, 0.0, "Breezy"),
            P.K,
            set(),
        ),
        (
            "wind at the critical threshold",
            W(60, crit_wind, 0.0, "Very windy"),
            P.QB,
            {"weather.wind"},
        ),
        (
            "high wind, position not wind-sensitive",
            W(60, crit_wind, 0.0, "Very windy"),
            P.RB,
            set(),
        ),
        (
            "precipitation above the threshold",
            W(55, 5, config.PRECIP_WARNING_CHANCE, "Rain"),
            P.RB,
            {"weather.precipitation"},
        ),
        (
            "extreme cold",
            W(config.COLD_WARNING_F, 5, 0.0, "Frigid"),
            P.QB,
            {"weather.cold"},
        ),
        (
            "extreme heat",
            W(config.HEAT_WARNING_F, 5, 0.0, "Hot"),
            P.RB,
            {"weather.heat"},
        ),
        (
            "wind and rain together",
            W(50, crit_wind, 0.9, "Storm"),
            P.WR,
            {"weather.wind", "weather.precipitation"},
        ),
    ]


def check_weather_scenarios() -> list[Issue]:
    """Run the weather rules across the scenario space.

    A data-quality check validates the data; this validates the *rules*. A
    threshold edited in `config.py` without a matching change here fails loudly
    instead of silently changing what gets flagged on a Sunday.
    """
    from datetime import timedelta

    import rules as rules_module
    from models import Game, Player, PlayerSlate

    kickoff = datetime.now(timezone.utc) + timedelta(days=1)
    venue = config.STADIUMS["KC"]
    issues: list[Issue] = []

    for label, weather, position, expected in _weather_scenarios():
        slate = PlayerSlate(
            player=Player("scenario", "Scenario Player", position, "KC"),
            game=Game("scenario", "KC", "BUF", kickoff, venue),
            weather=weather,
        )
        produced = {flag.code for flag in rules_module.check_weather(slate)}

        missing = expected - produced
        extra = produced - expected
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"expected but absent: {', '.join(sorted(missing))}")
            if extra:
                detail.append(f"unexpected: {', '.join(sorted(extra))}")
            issues.append(
                Issue(
                    IssueLevel.ERROR,
                    "weather.scenario_failed",
                    f"{label} ({position.value}) — " + "; ".join(detail),
                    "weather rules",
                )
            )

    return issues


# --- Swap legality ----------------------------------------------------------


def check_swap_legality() -> list[Issue]:
    """Every suggested swap must be one a person could actually make.

    A lineup slot accepts one position. The only exception is the FLEX, and
    those swaps must be labelled rather than left to look like an illegal
    substitution. This also confirms the swap deltas reconcile with the headline
    gap, since silently truncating the list makes the two disagree.

    Validates the comparison *logic*, not the data — the sibling of
    `check_weather_scenarios`.
    """
    from models import Platform, Position
    from sources.comp import CompUnavailable, build_user_lineup, compare, optimize
    from sources.odds import OddsUnavailable, get_book

    issues: list[Issue] = []

    try:
        book = get_book(use_props=False)
    except OddsUnavailable as exc:
        return [
            Issue(
                IssueLevel.NOTICE,
                "swap.unchecked",
                f"swap legality not verified: {exc}",
                "comp rules",
            )
        ]

    for platform in Platform:
        pool = book.pool_for(platform)
        rules = config.LINEUPS[platform]
        if not pool:
            issues.append(
                Issue(
                    IssueLevel.NOTICE,
                    "swap.unchecked",
                    f"no {platform.value} salary export, so swap legality is unverified",
                    "comp rules",
                )
            )
            continue

        try:
            optimal = optimize(pool, rules)
        except CompUnavailable as exc:
            issues.append(
                Issue(
                    IssueLevel.WARNING,
                    "swap.no_benchmark",
                    f"{platform.value}: cannot build an optimal lineup ({exc})",
                    "comp rules",
                )
            )
            continue

        # Degrade the optimal lineup into a plausible user lineup: keep the
        # positions, swap in the cheapest player available at each. That
        # guarantees a full slate of same-position swaps to inspect.
        by_position: dict[Position, list] = {}
        for candidate in pool:
            by_position.setdefault(candidate.player.position, []).append(candidate)
        for group in by_position.values():
            group.sort(key=lambda c: c.pricing.salary)

        names: list[str] = []
        used: set[str] = set()
        for entry in optimal.entries:
            for candidate in by_position.get(entry.player.position, []):
                if candidate.player.name not in used:
                    names.append(candidate.player.name)
                    used.add(candidate.player.name)
                    break

        user, missing = build_user_lineup(names, pool, rules)
        if missing:
            issues.append(
                Issue(
                    IssueLevel.NOTICE,
                    "swap.unchecked",
                    f"{platform.value}: could not assemble a test lineup",
                    "comp rules",
                )
            )
            continue

        comparison = compare(user, optimal, missing)

        for swap in comparison.swaps:
            out_position = swap.out_player.position
            in_position = swap.in_player.position

            if out_position is in_position:
                if swap.positional:
                    issues.append(
                        Issue(
                            IssueLevel.WARNING,
                            "swap.mislabelled",
                            f"{platform.value}: {swap.out_player.name} -> "
                            f"{swap.in_player.name} is same-position but marked "
                            "as a FLEX position change",
                            "comp rules",
                        )
                    )
                continue

            if not swap.positional:
                issues.append(
                    Issue(
                        IssueLevel.ERROR,
                        "swap.illegal",
                        f"{platform.value}: {out_position.value} "
                        f"{swap.out_player.name} -> {in_position.value} "
                        f"{swap.in_player.name} is not a move that can be made",
                        "comp rules",
                    )
                )
                continue

            # A labelled position change is only legal if both ends are
            # flex-eligible and the roster actually has a flex.
            if not rules.flex_count:
                issues.append(
                    Issue(
                        IssueLevel.ERROR,
                        "swap.no_flex",
                        f"{platform.value}: position change suggested on a roster "
                        "with no FLEX slot",
                        "comp rules",
                    )
                )
            elif not {out_position, in_position} <= rules.flex_eligible:
                ineligible = sorted(
                    p.value for p in {out_position, in_position} - rules.flex_eligible
                )
                issues.append(
                    Issue(
                        IssueLevel.ERROR,
                        "swap.flex_ineligible",
                        f"{platform.value}: FLEX swap involves "
                        f"{', '.join(ineligible)}, which cannot fill a FLEX",
                        "comp rules",
                    )
                )

        # The rows must reconcile with the headline, or the list was truncated.
        total = round(sum(s.points_delta for s in comparison.swaps), 2)
        if not comparison.unfilled and abs(total - comparison.points_gap) > 0.05:
            issues.append(
                Issue(
                    IssueLevel.ERROR,
                    "swap.totals_disagree",
                    f"{platform.value}: swaps sum to {total:+.1f} but the headline "
                    f"gap is {comparison.points_gap:+.1f} — swaps were dropped",
                    "comp rules",
                )
            )

    return issues


# --- Environment ------------------------------------------------------------


def check_environment() -> list[Issue]:
    """Validate configuration and data availability, independent of any slate."""
    issues: list[Issue] = []
    current_year = datetime.now(timezone.utc).year

    if current_year - config.SPLITS_SEASON >= 2:
        issues.append(
            Issue(
                IssueLevel.WARNING,
                "env.splits_stale",
                f"SPLITS_SEASON is {config.SPLITS_SEASON}, {current_year - config.SPLITS_SEASON} "
                "seasons behind — matchup flags reflect old defenses",
                "config",
            )
        )

    try:
        from sources.injuries import get_report

        report = get_report()
        if report.is_sparse:
            issues.append(
                Issue(
                    IssueLevel.WARNING,
                    "env.injury_report_sparse",
                    f"the {report.season} injury report has only {len(report)} rows — "
                    "designations are not filed yet, so every injury check will "
                    "report clean regardless of who is hurt",
                    "config",
                )
            )
    except Exception:
        pass

    if not config.ODDS_API_KEY:
        issues.append(
            Issue(
                IssueLevel.NOTICE,
                "env.no_odds_key",
                "ODDS_API_KEY unset — projections fall back to salary-export averages",
                "config",
            )
        )

    if not config.SALARY_DIR.exists():
        issues.append(
            Issue(
                IssueLevel.WARNING,
                "env.no_salary_dir",
                f"{config.SALARY_DIR}/ does not exist — no value flags possible",
                "config",
            )
        )
    elif not list(config.SALARY_DIR.glob("*.csv")):
        issues.append(
            Issue(
                IssueLevel.WARNING,
                "env.no_salary_exports",
                f"no CSV exports in {config.SALARY_DIR}/ — see its README",
                "config",
            )
        )

    if config.NWS_USER_AGENT.endswith("set NWS_USER_AGENT)"):
        issues.append(
            Issue(
                IssueLevel.NOTICE,
                "env.default_user_agent",
                "NWS_USER_AGENT is the placeholder; NWS asks for a real contact address",
                "config",
            )
        )

    return issues


# --- Runner -----------------------------------------------------------------

PER_SLATE_CHECKS = [
    check_slate_integrity,
    check_weather_present,
    check_weather_sane,
    check_split_present,
    check_pricing_present,
    check_pricing_sane,
]

# Checks that need the week being played, and the network to resolve it.
PER_SLATE_WEEK_CHECKS = [
    check_active_that_week,
    check_injury_week,
]

_LEVEL_ORDER = {IssueLevel.ERROR: 0, IssueLevel.WARNING: 1, IssueLevel.NOTICE: 2}


@dataclass
class QAReport:
    issues: list[Issue]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level is IssueLevel.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level is IssueLevel.WARNING]

    @property
    def notices(self) -> list[Issue]:
        return [i for i in self.issues if i.level is IssueLevel.NOTICE]

    @property
    def ok(self) -> bool:
        """True when nothing would render wrong. Warnings are tolerable."""
        return not self.errors

    def format(self) -> str:
        if not self.issues:
            return "QA: no issues."

        lines = [str(issue) for issue in self.issues]
        lines.append("")
        lines.append(
            f"QA: {len(self.errors)} error(s), {len(self.warnings)} warning(s), "
            f"{len(self.notices)} notice(s)."
        )
        return "\n".join(lines)


def validate(
    slates: list[PlayerSlate],
    include_injuries: bool = True,
    include_environment: bool = True,
) -> QAReport:
    """Run every check over a slate list and return a sorted report."""
    issues: list[Issue] = []

    if include_environment:
        issues.extend(check_environment())
        issues.extend(check_weather_scenarios())
        issues.extend(check_swap_legality())
        try:
            import overrides

            issues.extend(overrides.validate())
        except Exception as exc:  # a broken overrides file must not abort QA
            issues.append(
                Issue(
                    IssueLevel.WARNING,
                    "override.unreadable",
                    f"overrides.json could not be read: {exc}",
                    "overrides",
                )
            )

    issues.extend(check_duplicates(slates))

    for slate in slates:
        for check in PER_SLATE_CHECKS:
            issues.extend(check(slate))
        if include_injuries:
            issues.extend(check_injury(slate))
            issues.extend(check_roster_status(slate))
            for check in PER_SLATE_WEEK_CHECKS:
                issues.extend(check(slate))

    issues.sort(key=lambda i: (_LEVEL_ORDER[i.level], i.subject, i.code))
    return QAReport(issues)


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point: validate the demo slate and exit non-zero on errors."""
    import argparse

    parser = argparse.ArgumentParser(description="Validate slate data quality.")
    parser.add_argument("--no-injuries", action="store_true", help="skip the injury report")
    parser.add_argument("--no-environment", action="store_true", help="skip config checks")
    args = parser.parse_args(argv)

    import main as pipeline

    slates = pipeline.build_demo_slates()
    pipeline.enrich_with_weather(slates, quiet=True)
    pipeline.enrich_with_splits(slates, quiet=True)
    pipeline.enrich_with_pricing(slates, quiet=True)

    report = validate(
        slates,
        include_injuries=not args.no_injuries,
        include_environment=not args.no_environment,
    )
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
