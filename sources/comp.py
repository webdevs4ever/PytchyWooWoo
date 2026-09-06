"""Comp Mode — compare a user's lineup against the optimal one.

The user's lineup is the subject; the optimal lineup is the benchmark. The
output is a diff: which players to swap, what each swap costs in salary and
gains in projected points, and which of their picks carry injury or weather
flags.

## On the optimizer

This is a constrained knapsack, not a sort. Picking greedily by points per
dollar does not produce the optimal lineup — a cheap high-rate player can crowd
out a pairing that scores more within the same cap. The exact solution here is:

1. Resolve the FLEX by trying each eligible position in turn, which turns the
   problem into a fixed requirement per position.
2. Per position, run a small knapsack over salary for "exactly k players".
3. Convolve the position tables together under the cap.

Each table is pruned to its Pareto frontier — for a given salary, only the
best-scoring selection survives — which keeps the convolution tractable on a
full slate.
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sources"

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
from models import (
    Game,
    Lineup,
    LineupEntry,
    LineupRules,
    Platform,
    Player,
    Position,
    Severity,
)
from sources.odds import Candidate

# "BUF@KC 09/13/2026 01:00PM ET" — real contest exports carry this; it is the
# only link from a salary row back to a kickoff, and so to a forecast.
GAME_INFO = re.compile(r"^([A-Z]{2,3})@([A-Z]{2,3})\s+(\d{2}/\d{2}/\d{4})")


class CompUnavailable(Exception):
    """Raised when a lineup cannot be built or compared."""


# --- Optimizer --------------------------------------------------------------

# salary units -> (points, tuple of candidate indices)
Table = dict[int, tuple[float, tuple[int, ...]]]


def _prune(table: Table) -> Table:
    """Keep only the Pareto frontier: cheapest selection for each点 level.

    Without this the convolution below multiplies out to millions of dominated
    entries on a full slate.
    """
    best = -math.inf
    kept: Table = {}
    for salary in sorted(table):
        points, picks = table[salary]
        if points > best:
            best = points
            kept[salary] = (points, picks)
    return kept


def _position_table(
    candidates: list[Candidate], indices: list[int], k: int, cap_units: int, unit: int
) -> Table:
    """Best selection of exactly `k` players from one position, by salary."""
    dp: list[Table] = [dict() for _ in range(k + 1)]
    dp[0][0] = (0.0, ())

    for idx in indices:
        price = candidates[idx].pricing
        cost = price.salary // unit
        points = price.projected_points
        for filled in range(k - 1, -1, -1):
            for salary, (total, picks) in list(dp[filled].items()):
                new_salary = salary + cost
                if new_salary > cap_units:
                    continue
                new_points = total + points
                current = dp[filled + 1].get(new_salary)
                if current is None or new_points > current[0]:
                    dp[filled + 1][new_salary] = (new_points, picks + (idx,))

    return _prune(dp[k])


def _combine(left: Table, right: Table, cap_units: int) -> Table:
    """Convolve two position tables under the salary cap."""
    out: Table = {}
    for salary_a, (points_a, picks_a) in left.items():
        for salary_b, (points_b, picks_b) in right.items():
            total_salary = salary_a + salary_b
            if total_salary > cap_units:
                continue
            total_points = points_a + points_b
            current = out.get(total_salary)
            if current is None or total_points > current[0]:
                out[total_salary] = (total_points, picks_a + picks_b)
    return _prune(out)


def playable_pool(candidates: list[Candidate], quiet: bool = True) -> list[Candidate]:
    """Drop players ruled out on the injury report.

    An optimizer that maximises projection alone will happily build a lineup
    around someone who will not take a snap — their salary is cheap precisely
    because they are out. Filtering before optimising is the only way the
    benchmark means anything.
    """
    try:
        from sources.injuries import InjuriesUnavailable, fetch_status
    except ImportError:  # pragma: no cover
        return candidates

    kept: list[Candidate] = []
    for candidate in candidates:
        try:
            status = fetch_status(candidate.player.name)
        except InjuriesUnavailable:
            return candidates  # no report; better a full pool than none
        if status is not None and status.is_ruled_out:
            continue
        kept.append(candidate)
    return kept


def optimize(
    candidates: list[Candidate], rules: LineupRules, exclude_out: bool = True
) -> Lineup:
    """The highest-projecting valid lineup under `rules`.

    Players ruled out are excluded by default; pass `exclude_out=False` to
    optimise over the raw pool.

    Raises `CompUnavailable` when the pool cannot fill the roster.
    """
    if not candidates:
        raise CompUnavailable("No priced players available")

    if exclude_out:
        candidates = playable_pool(candidates)
        if not candidates:
            raise CompUnavailable("Every priced player is ruled out")

    by_position: dict[Position, list[int]] = {}
    for idx, candidate in enumerate(candidates):
        by_position.setdefault(candidate.player.position, []).append(idx)

    salaries = [c.pricing.salary for c in candidates]
    unit = math.gcd(*salaries) if len(salaries) > 1 else (salaries[0] or 1)
    unit = max(unit, 1)
    cap_units = rules.salary_cap // unit

    flex_choices = sorted(rules.flex_eligible, key=lambda p: p.value) or [None]
    best: tuple[float, tuple[int, ...]] | None = None

    for flex_position in flex_choices:
        requirements = dict(rules.requirements)
        if flex_position is not None and rules.flex_count:
            requirements[flex_position] = requirements.get(flex_position, 0) + rules.flex_count

        tables: list[Table] = []
        feasible = True
        for position, count in requirements.items():
            pool = by_position.get(position, [])
            if len(pool) < count:
                feasible = False
                break
            tables.append(_position_table(candidates, pool, count, cap_units, unit))
        if not feasible or not tables:
            continue

        combined = tables[0]
        for table in tables[1:]:
            combined = _combine(combined, table, cap_units)
            if not combined:
                break
        if not combined:
            continue

        salary, (points, picks) = max(combined.items(), key=lambda kv: kv[1][0])
        if best is None or points > best[0]:
            best = (points, picks)

    if best is None:
        raise CompUnavailable(
            f"Pool cannot fill a {rules.platform.value} roster "
            f"({rules.total_slots} slots under ${rules.salary_cap:,})"
        )

    return _to_lineup([candidates[i] for i in best[1]], rules)


def _to_lineup(chosen: list[Candidate], rules: LineupRules) -> Lineup:
    """Assign chosen players to named slots, flex last."""
    remaining = sorted(chosen, key=lambda c: -c.pricing.projected_points)
    entries: list[LineupEntry] = []

    for position in (Position.QB, Position.RB, Position.WR, Position.TE, Position.DST):
        needed = rules.requirements.get(position, 0)
        if not needed:
            continue
        matches = [c for c in remaining if c.player.position is position][:needed]
        for i, candidate in enumerate(matches):
            label = position.value if needed == 1 else f"{position.value}{i + 1}"
            entries.append(
                LineupEntry(label, candidate.player, candidate.pricing.salary,
                            candidate.pricing.projected_points)
            )
            remaining.remove(candidate)

    for i, candidate in enumerate(remaining):
        label = "FLEX" if rules.flex_count == 1 else f"FLEX{i + 1}"
        entries.append(
            LineupEntry(label, candidate.player, candidate.pricing.salary,
                        candidate.pricing.projected_points)
        )

    return Lineup(platform=rules.platform, entries=entries)


# --- User lineups -----------------------------------------------------------


def parse_lineup(text: str) -> list[str]:
    """Read player names from an uploaded lineup.

    Accepts one name per line, or a CSV whose first column is the name. Blank
    lines, comments, and a header row are ignored.
    """
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        first = line.split(",")[0].strip().strip('"')
        if first.lower() in {"name", "player", "nickname"}:
            continue
        if first:
            names.append(first)
    return names


def build_user_lineup(
    names: list[str], candidates: list[Candidate], rules: LineupRules
) -> tuple[Lineup, list[str]]:
    """Match uploaded names against the priced pool.

    Returns the lineup and any names that could not be matched — an unmatched
    name is reported rather than silently dropped, because a lineup missing a
    slot would otherwise look cheaper and worse than it is.
    """
    from sources.odds import strict_name

    index: dict[str, Candidate] = {}
    for candidate in candidates:
        index.setdefault(strict_name(candidate.player.name), candidate)

    chosen: list[Candidate] = []
    missing: list[str] = []
    for name in names:
        found = index.get(strict_name(name))
        if found is None:
            missing.append(name)
        else:
            chosen.append(found)

    return _to_lineup(chosen, rules), missing


# --- Comparison -------------------------------------------------------------


@dataclass
class Swap:
    out_player: Player
    in_player: Player
    salary_delta: int
    points_delta: float
    # Set when the outgoing player will not play, which makes a negative
    # points_delta misleading — their real projection is zero, not what the
    # salary export last recorded.
    note: str = ""


@dataclass
class Comparison:
    platform: Platform
    user: Lineup
    optimal: Lineup
    missing: list[str] = field(default_factory=list)
    kept: list[LineupEntry] = field(default_factory=list)
    swaps: list[Swap] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    @property
    def points_gap(self) -> float:
        return round(self.optimal.projected_points - self.user.projected_points, 2)

    @property
    def over_cap(self) -> int:
        cap = config.LINEUPS[self.platform].salary_cap
        return max(0, self.user.salary - cap)


def compare(user: Lineup, optimal: Lineup, missing: list[str] | None = None) -> Comparison:
    """Diff a user's lineup against the optimal one."""
    user_by_id = {e.player.player_id: e for e in user.entries}
    optimal_by_id = {e.player.player_id: e for e in optimal.entries}

    shared = user_by_id.keys() & optimal_by_id.keys()
    kept = [user_by_id[pid] for pid in shared]

    dropped = sorted(
        (e for pid, e in user_by_id.items() if pid not in shared),
        key=lambda e: e.projected_points,
    )
    added = sorted(
        (e for pid, e in optimal_by_id.items() if pid not in shared),
        key=lambda e: e.projected_points,
    )

    swaps = [
        Swap(
            out_player=out.player,
            in_player=into.player,
            salary_delta=into.salary - out.salary,
            points_delta=round(into.projected_points - out.projected_points, 2),
        )
        for out, into in zip(dropped, added)
    ]
    _mark_unplayable(swaps)
    swaps.sort(key=lambda s: -s.points_delta)

    return Comparison(
        platform=user.platform,
        user=user,
        optimal=optimal,
        missing=list(missing or []),
        kept=sorted(kept, key=lambda e: -e.projected_points),
        swaps=swaps,
    )


def _mark_unplayable(swaps: list[Swap]) -> None:
    """Note swaps whose outgoing player is ruled out.

    Their projection in the salary export is stale — it reflects a season
    average, not the zero they will actually score — so the raw delta reads as a
    downgrade when it is the opposite.
    """
    try:
        from sources.injuries import InjuriesUnavailable, fetch_status
    except ImportError:  # pragma: no cover
        return

    for swap in swaps:
        try:
            status = fetch_status(swap.out_player.name)
        except InjuriesUnavailable:
            return
        if status is not None and status.is_ruled_out:
            swap.note = "outgoing player is OUT — real projection is 0.0"


def annotate(comparison: Comparison, quiet: bool = True) -> None:
    """Attach injury and weather alerts for the user's own picks.

    Weather needs a kickoff, which only reaches us through the export's
    Game Info column. When that column is absent or unparsable, weather is
    skipped and injuries still run.
    """
    from sources.injuries import InjuriesUnavailable, fetch_status

    alerts: list[str] = []

    for entry in comparison.user.entries:
        try:
            status = fetch_status(entry.player.name)
        except InjuriesUnavailable:
            break
        if status is None:
            continue
        if status.is_ruled_out:
            alerts.append(f"{entry.player.name} ({entry.slot}) is ruled OUT — will score zero")
        elif status.is_doubtful:
            alerts.append(f"{entry.player.name} ({entry.slot}) is doubtful")
        elif status.is_questionable:
            alerts.append(f"{entry.player.name} ({entry.slot}) is questionable")

    for entry in comparison.user.entries:
        game = getattr(entry.player, "_game", None)
        if game is None:
            continue
        try:
            from sources.weather import WeatherUnavailable, fetch_forecast

            weather = fetch_forecast(game)
        except Exception:
            continue
        if weather.is_indoor:
            continue
        if weather.wind_mph >= config.WIND_WARNING_MPH:
            alerts.append(
                f"{entry.player.name} ({entry.slot}): {weather.wind_mph:.0f} mph wind"
            )
        if weather.precipitation_chance >= config.PRECIP_WARNING_CHANCE:
            alerts.append(
                f"{entry.player.name} ({entry.slot}): "
                f"{weather.precipitation_chance:.0%} chance of precipitation"
            )

    comparison.alerts = alerts


def game_from_info(info: str, home_fallback: str | None = None) -> Game | None:
    """Build a `Game` from a contest export's Game Info column, if parsable."""
    match = GAME_INFO.match((info or "").strip())
    if not match:
        return None
    away, home, date_text = match.groups()
    venue = config.venue_for(home)
    if venue is None:
        return None
    try:
        kickoff = datetime.strptime(date_text, "%m/%d/%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return Game(f"{away}-{home}", home, away, kickoff, venue)


# --- CLI --------------------------------------------------------------------


def format_lineup(lineup: Lineup, cap: int, title: str) -> str:
    lines = [f"{title}  —  {lineup.projected_points} proj, ${lineup.salary:,} of ${cap:,}"]
    for entry in lineup.entries:
        lines.append(
            f"  {entry.slot:5} {entry.player.name:24} {entry.player.team:4} "
            f"${entry.salary:>6,}  {entry.projected_points:5.1f}"
        )
    return "\n".join(lines)


def format_comparison(comparison: Comparison) -> str:
    rules = config.LINEUPS[comparison.platform]
    parts = [
        format_lineup(comparison.user, rules.salary_cap, "Your lineup"),
        "",
        format_lineup(comparison.optimal, rules.salary_cap, "Optimal lineup"),
        "",
    ]

    if comparison.missing:
        parts.append(
            "Not found in the salary export: " + ", ".join(comparison.missing)
        )
        parts.append("")

    if comparison.over_cap:
        parts.append(f"Over the cap by ${comparison.over_cap:,} — this lineup is invalid.")
        parts.append("")

    if not comparison.swaps:
        parts.append("No swaps — your lineup is already optimal.")
    else:
        parts.append(f"Suggested swaps ({comparison.points_gap:+} projected points):")
        for swap in comparison.swaps:
            direction = "+" if swap.salary_delta >= 0 else "-"
            parts.append(
                f"  OUT {swap.out_player.name:24} -> IN {swap.in_player.name:24} "
                f"{swap.points_delta:+6.1f} pts   {direction}${abs(swap.salary_delta):,}"
            )
            if swap.note:
                parts.append(f"        {swap.note}")

    if comparison.kept:
        parts.append("")
        parts.append(f"Keeping {len(comparison.kept)}: "
                     + ", ".join(e.player.name for e in comparison.kept))

    if comparison.alerts:
        parts.append("")
        parts.append("Alerts on your picks:")
        parts.extend(f"  ! {alert}" for alert in comparison.alerts)

    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    from sources.odds import get_book

    parser = argparse.ArgumentParser(description="Compare a lineup against the optimal one.")
    parser.add_argument("lineup", nargs="?", help="file of player names, one per line")
    parser.add_argument(
        "--platform",
        choices=[p.value for p in Platform],
        default=Platform.DRAFTKINGS.value,
    )
    parser.add_argument("--optimal", action="store_true", help="just show the optimal lineup")
    parser.add_argument("--no-props", action="store_true", help="skip live player props")
    parser.add_argument("--no-alerts", action="store_true", help="skip injury lookups")
    parser.add_argument(
        "--include-out",
        action="store_true",
        help="let the optimal lineup use players who are ruled out",
    )
    args = parser.parse_args(argv)

    platform = Platform(args.platform)
    rules = config.LINEUPS[platform]

    book = get_book(use_props=not args.no_props)
    pool = book.pool_for(platform)
    if not pool:
        print(
            f"No {platform.value} salary export found in {config.SALARY_DIR}/ — "
            "see its README.",
        )
        return 1

    try:
        optimal = optimize(pool, rules, exclude_out=not args.include_out)
    except CompUnavailable as exc:
        print(f"Could not build a lineup: {exc}")
        return 1

    if args.optimal or not args.lineup:
        print(format_lineup(optimal, rules.salary_cap, f"Optimal {platform.value} lineup"))
        return 0

    path = Path(args.lineup)
    if not path.exists():
        print(f"No such lineup file: {path}")
        return 1

    names = parse_lineup(path.read_text(encoding="utf-8"))
    user, missing = build_user_lineup(names, pool, rules)
    comparison = compare(user, optimal, missing)
    if not args.no_alerts:
        annotate(comparison)

    print(format_comparison(comparison))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
