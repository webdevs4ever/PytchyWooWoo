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
from dataclasses import replace
from models import Game, Player, PlayerSlate, Position
from sources.odds import OddsUnavailable, get_book
from sources.narratives import NarrativesUnavailable, for_slate, get_history
from sources.rosters import RostersUnavailable
from sources.rosters import get_book as get_roster_book
from sources.splits import SplitsUnavailable, fetch_split
from sources.weather import WeatherUnavailable, fetch_forecast

def build_demo_slates() -> list[PlayerSlate]:
    """A hand-built slate, kicking off inside the NWS forecast window.

    Stands in for a real schedule source. Splits, weather, rosters, and pricing
    are attached separately by the enrich_* functions.
    """
    kickoff = datetime.now(timezone.utc) + timedelta(days=3)

    # Season and week are carried so QA can align injury designations and
    # roster status to the week actually being played.
    season, week = 2026, 1

    def game(game_id: str, home: str, away: str) -> Game:
        return Game(
            game_id=game_id,
            home_team=home,
            away_team=away,
            kickoff=kickoff,
            venue=config.STADIUMS[home],
            season=season,
            week=week,
        )

    buf_at_kc = game("2026-W1-BUF-KC", "KC", "BUF")
    lv_at_atl = game("2026-W1-LV-ATL", "ATL", "LV")
    no_at_jax = game("2026-W1-NO-JAX", "JAX", "NO")
    min_at_pit = game("2026-W1-MIN-PIT", "PIT", "MIN")
    bal_at_det = game("2026-W1-BAL-DET", "DET", "BAL")

    # Chosen to exercise every position group and both narrative types.
    roster = [
        # QB
        ("mahomes", "Patrick Mahomes", Position.QB, "KC", buf_at_kc),
        ("allen", "Josh Allen", Position.QB, "BUF", buf_at_kc),
        ("cousins", "Kirk Cousins", Position.QB, "LV", lv_at_atl),
        # RB
        ("white", "Zamir White", Position.RB, "NO", no_at_jax),
        # WR
        ("thielen", "Adam Thielen", Position.WR, "MIN", min_at_pit),
        ("stbrown", "Amon-Ra St. Brown", Position.WR, "DET", bal_at_det),
        # K
        ("butker", "Harrison Butker", Position.K, "KC", buf_at_kc),
        ("tucker", "Justin Tucker", Position.K, "BAL", bal_at_det),
        # DST
        ("bal_dst", "Ravens D/ST", Position.DST, "BAL", bal_at_det),
    ]

    return [
        PlayerSlate(player=Player(pid, name, pos, team), game=g)
        for pid, name, pos, team, g in roster
    ]


def enrich_with_rosters(slates: list[PlayerSlate], quiet: bool = False) -> None:
    """Attach jersey numbers from the roster feed, in place."""
    try:
        book = get_roster_book()
    except RostersUnavailable as exc:
        if not quiet:
            print(f"  ! rosters unavailable: {exc}", file=sys.stderr)
        return

    for slate in slates:
        entry = book.entry_for(slate.player.name, slate.player.team)
        if entry and entry.jersey_number:
            slate.player = replace(slate.player, number=entry.jersey_number)


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


def enrich_with_narratives(slates: list[PlayerSlate], quiet: bool = False) -> None:
    """Attach matchup stories in place. One history load serves every lookup."""
    try:
        history = get_history()
    except NarrativesUnavailable as exc:
        if not quiet:
            print(f"  ! narratives unavailable: {exc}", file=sys.stderr)
        return

    for slate in slates:
        slate.narratives = for_slate(slate, history)


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

        number = f"#{slate.player.number} " if slate.player.number else ""
        print(f"\n{number}{slate.player}  —  {slate.game}")
        if conditions:
            print(conditions)

        if not flags:
            print("  (no flags)")
        for flag in flags:
            print(f"  {flag.severity.value.upper():8} {flag.code:24} {flag.reason}")
        for story in slate.narratives:
            print(f"  {'STORY':8} {story.kind.value:24} {story.headline} — {story.detail}")

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
        "--no-rosters",
        action="store_true",
        help="skip the roster download (jersey numbers unavailable)",
    )
    parser.add_argument(
        "--no-narratives",
        action="store_true",
        help="skip Narrative Street (roster history download)",
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

    if not args.no_rosters:
        enrich_with_rosters(slates, quiet=args.quiet)
    if not args.no_weather:
        enrich_with_weather(slates, quiet=args.quiet)
    if not args.no_splits:
        enrich_with_splits(slates, quiet=args.quiet)
    if not args.no_pricing:
        enrich_with_pricing(slates, use_props=not args.no_props, quiet=args.quiet)
    if not args.no_narratives:
        enrich_with_narratives(slates, quiet=args.quiet)

    if args.qa or args.qa_strict:
        from sources import qa

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
