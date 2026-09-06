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

    issues.extend(check_duplicates(slates))

    for slate in slates:
        for check in PER_SLATE_CHECKS:
            issues.extend(check(slate))
        if include_injuries:
            issues.extend(check_injury(slate))
            issues.extend(check_roster_status(slate))

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
