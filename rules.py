"""The flagging engine.

Each check is an independent function over a `PlayerSlate`, returning zero or
more `Flag`s. They share no state and can be run in any order, so adding a new
signal means writing one function and listing it in `ALL_CHECKS`.

A check returns an empty list when it has nothing to say — including when its
data source is missing. Absent data is not a flag.
"""

from __future__ import annotations

from typing import Callable

import config
from models import Flag, Platform, PlayerSlate, Position, Severity

# Positions whose production depends most on conditions.
WIND_SENSITIVE = {Position.K, Position.QB, Position.WR, Position.TE}


def check_weather(slate: PlayerSlate) -> list[Flag]:
    """Flag wind, precipitation, and temperature extremes at kickoff."""
    weather = slate.weather
    if weather is None or weather.is_indoor:
        return []

    flags: list[Flag] = []

    if weather.wind_mph >= config.WIND_CRITICAL_MPH:
        severity = Severity.CRITICAL
    elif weather.wind_mph >= config.WIND_WARNING_MPH:
        severity = Severity.WARNING
    else:
        severity = None

    if severity and slate.player.position in WIND_SENSITIVE:
        flags.append(
            Flag(
                code="weather.wind",
                severity=severity,
                reason=f"{weather.wind_mph:.0f} mph wind at kickoff "
                f"({slate.player.position.value} is wind-sensitive)",
                player=slate.player,
                game=slate.game,
            )
        )

    if weather.precipitation_chance >= config.PRECIP_WARNING_CHANCE:
        flags.append(
            Flag(
                code="weather.precipitation",
                severity=Severity.WARNING,
                reason=f"{weather.precipitation_chance:.0%} chance of precipitation "
                f"({weather.description})",
                player=slate.player,
                game=slate.game,
            )
        )

    if weather.temperature_f <= config.COLD_WARNING_F:
        flags.append(
            Flag(
                code="weather.cold",
                severity=Severity.WARNING,
                reason=f"{weather.temperature_f:.0f}°F at kickoff",
                player=slate.player,
                game=slate.game,
            )
        )
    elif weather.temperature_f >= config.HEAT_WARNING_F:
        flags.append(
            Flag(
                code="weather.heat",
                severity=Severity.INFO,
                reason=f"{weather.temperature_f:.0f}°F at kickoff",
                player=slate.player,
                game=slate.game,
            )
        )

    return flags


def check_matchup_split(slate: PlayerSlate) -> list[Flag]:
    """Flag opponents giving up notably more or fewer points to this position."""
    split = slate.split
    if split is None or split.games_sampled < config.SPLIT_MIN_GAMES:
        return []

    delta = split.delta
    magnitude = abs(delta)
    if magnitude < config.SPLIT_WARNING_DELTA:
        return []

    severity = (
        Severity.CRITICAL if magnitude >= config.SPLIT_CRITICAL_DELTA else Severity.WARNING
    )
    direction = "above" if delta > 0 else "below"
    code = "matchup.favorable" if delta > 0 else "matchup.difficult"

    return [
        Flag(
            code=code,
            severity=severity,
            reason=f"{split.opponent} allows {split.fantasy_points_allowed:.1f} FP to "
            f"{split.position.value}s, {magnitude:.1f} {direction} league average "
            f"(n={split.games_sampled})",
            player=slate.player,
            game=slate.game,
        )
    ]


def check_price_value(slate: PlayerSlate) -> list[Flag]:
    """Flag under- and overpricing, evaluated per platform.

    DraftKings and FanDuel price independently, so a player can be a bargain on
    one site and a trap on the other. That divergence is the point.
    """
    flags: list[Flag] = []

    for platform, pricing in slate.pricing.items():
        ppk = pricing.points_per_thousand
        if ppk >= config.VALUE_GOOD_PPK:
            flags.append(
                Flag(
                    code="value.underpriced",
                    severity=Severity.INFO,
                    reason=f"${pricing.salary:,} for {pricing.projected_points:.1f} "
                    f"proj = {ppk:.2f} pts/$1k (good value)",
                    player=slate.player,
                    game=slate.game,
                    platform=platform,
                )
            )
        elif ppk <= config.VALUE_POOR_PPK:
            flags.append(
                Flag(
                    code="value.overpriced",
                    severity=Severity.WARNING,
                    reason=f"${pricing.salary:,} for {pricing.projected_points:.1f} "
                    f"proj = {ppk:.2f} pts/$1k (poor value)",
                    player=slate.player,
                    game=slate.game,
                    platform=platform,
                )
            )

    return flags


def check_platform_divergence(slate: PlayerSlate) -> list[Flag]:
    """Flag players priced very differently across platforms.

    Groundwork for Comps Mode: these are the players where the optimal DK and
    FanDuel lineups are most likely to disagree.
    """
    dk = slate.pricing.get(Platform.DRAFTKINGS)
    fd = slate.pricing.get(Platform.FANDUEL)
    if not dk or not fd:
        return []

    gap = dk.points_per_thousand - fd.points_per_thousand
    if abs(gap) < 0.5:
        return []

    better = "DraftKings" if gap > 0 else "FanDuel"
    return [
        Flag(
            code="value.divergence",
            severity=Severity.INFO,
            reason=f"Better value on {better} "
            f"(DK {dk.points_per_thousand:.2f} vs FD {fd.points_per_thousand:.2f} pts/$1k)",
            player=slate.player,
            game=slate.game,
        )
    ]


ALL_CHECKS: list[Callable[[PlayerSlate], list[Flag]]] = [
    check_weather,
    check_matchup_split,
    check_price_value,
    check_platform_divergence,
]

_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


def evaluate(slate: PlayerSlate) -> list[Flag]:
    """Run every check against one slate, most severe flags first."""
    flags = [flag for check in ALL_CHECKS for flag in check(slate)]
    return sorted(flags, key=lambda f: _SEVERITY_ORDER[f.severity])
