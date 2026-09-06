"""CLI entry point.

Runs the ingestion → normalization → rules pipeline and prints the flags.

Only weather is wired to a live source today; splits and pricing are stubs, so
the demo slate supplies sample values for those to exercise the full rules
engine end to end. Replace `build_demo_slates()` with a real schedule source
once `sources/splits.py` and `sources/odds.py` are implemented.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import config
import rules
from models import Game, Player, PlayerSlate, Position
from sources.odds import OddsUnavailable, get_book
from sources.splits import SplitsUnavailable, fetch_split
from sources.weather import WeatherUnavailable, fetch_forecast

def build_demo_slates() -> list[PlayerSlate]:
    """A small hand-built slate, kicking off inside the NWS forecast window.

    Splits and weather are attached separately by the enrich_* functions.
    """
    kickoff = datetime.now(timezone.utc) + timedelta(days=3)

    buf_at_kc = Game(
        game_id="2026-W1-BUF-KC",
        home_team="KC",
        away_team="BUF",
        kickoff=kickoff,
        venue=config.STADIUMS["KC"],
    )
    bal_at_det = Game(
        game_id="2026-W1-BAL-DET",
        home_team="DET",
        away_team="BAL",
        kickoff=kickoff,
        venue=config.STADIUMS["DET"],
    )

    return [
        PlayerSlate(
            player=Player("mahomes", "Patrick Mahomes", Position.QB, "KC"),
            game=buf_at_kc,
        ),
        PlayerSlate(
            player=Player("allen", "Josh Allen", Position.QB, "BUF"),
            game=buf_at_kc,
        ),
        PlayerSlate(
            player=Player("tucker", "Justin Tucker", Position.K, "BAL"),
            game=bal_at_det,
        ),
    ]


def enrich_with_weather(slates: list[PlayerSlate], quiet: bool = False) -> None:
    """Attach forecasts in place, caching one lookup per game."""
    cache: dict[str, object] = {}

    for slate in slates:
        game_id = slate.game.game_id
        if game_id not in cache:
            try:
                cache[game_id] = fetch_forecast(slate.game)
            except WeatherUnavailable as exc:
                if not quiet:
                    print(f"  ! weather unavailable for {slate.game}: {exc}", file=sys.stderr)
                cache[game_id] = None
        slate.weather = cache[game_id]


def opponent_of(slate: PlayerSlate) -> str:
    """The defense a player faces: whichever side of the game isn't their own."""
    game = slate.game
    return game.home_team if slate.player.team == game.away_team else game.away_team


def enrich_with_splits(slates: list[PlayerSlate], quiet: bool = False) -> None:
    """Attach defensive splits in place. One table load serves every lookup."""
    for slate in slates:
        try:
            slate.split = fetch_split(opponent_of(slate), slate.player.position)
        except SplitsUnavailable as exc:
            if not quiet:
                print(f"  ! splits unavailable: {exc}", file=sys.stderr)
            return


def enrich_with_pricing(
    slates: list[PlayerSlate], use_props: bool = True, quiet: bool = False
) -> None:
    """Attach salary and projection per platform, in place."""
    try:
        book = get_book(use_props=use_props)
    except OddsUnavailable as exc:
        if not quiet:
            print(f"  ! pricing unavailable: {exc}", file=sys.stderr)
        return

    if book.props_error and not quiet:
        print(f"  ! props unavailable, using season averages: {book.props_error}",
              file=sys.stderr)
    if not book.salaries and not quiet:
        print(f"  ! no salary exports found in {config.SALARY_DIR}/ — see its README",
              file=sys.stderr)

    for slate in slates:
        slate.pricing = book.pricing_for(slate.player)


def report(slates: list[PlayerSlate]) -> int:
    """Print flags grouped by player. Returns the number of flags raised."""
    total = 0

    for slate in slates:
        flags = rules.evaluate(slate)
        total += len(flags)

        conditions = ""
        if slate.weather:
            w = slate.weather
            conditions = (
                "  indoor"
                if w.is_indoor
                else f"  {w.temperature_f:.0f}°F, {w.wind_mph:.0f} mph wind, "
                f"{w.precipitation_chance:.0%} precip — {w.description}"
            )

        print(f"\n{slate.player}  —  {slate.game}")
        if conditions:
            print(conditions)

        if not flags:
            print("  (no flags)")
        for flag in flags:
            print(f"  {flag.severity.value.upper():8} {flag.code:24} {flag.reason}")

    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flag weather, matchup, and pricing signals.")
    parser.add_argument(
        "--no-weather",
        action="store_true",
        help="skip live NWS calls (useful offline)",
    )
    parser.add_argument(
        "--no-splits",
        action="store_true",
        help="skip the nflverse splits download (useful offline)",
    )
    parser.add_argument(
        "--no-pricing",
        action="store_true",
        help="skip salary/props loading",
    )
    parser.add_argument(
        "--no-props",
        action="store_true",
        help="use salary-export season averages instead of live player props",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help=f"season for split data (default {config.SPLITS_SEASON})",
    )
    parser.add_argument(
        "--qa",
        action="store_true",
        help="run data-quality validation before the report",
    )
    parser.add_argument(
        "--qa-strict",
        action="store_true",
        help="run validation and exit non-zero if it finds errors",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress source warnings")
    args = parser.parse_args(argv)

    slates = build_demo_slates()

    if args.season is not None:
        config.SPLITS_SEASON = args.season

    if not args.no_weather:
        enrich_with_weather(slates, quiet=args.quiet)
    if not args.no_splits:
        enrich_with_splits(slates, quiet=args.quiet)
    if not args.no_pricing:
        enrich_with_pricing(slates, use_props=not args.no_props, quiet=args.quiet)

    if args.qa or args.qa_strict:
        import qa

        qa_report = qa.validate(slates)
        print("=" * 72)
        print("Data quality")
        print("=" * 72)
        print(qa_report.format())
        print()
        if args.qa_strict and not qa_report.ok:
            print("Aborting: validation found errors.", file=sys.stderr)
            return 1

    print("=" * 72)
    print("Fantasy Bet Helper — flag report")
    if not args.no_splits:
        print(f"splits: {config.SPLITS_SEASON} season, {config.SPLITS_SCORING} scoring")
    print("=" * 72)

    total = report(slates)

    print(f"\n{total} flag(s) across {len(slates)} player(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
