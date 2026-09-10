"""Grade a set of Kalshi/Polymarket positions.

Takes uploaded predictions and returns, for each one:

- a **verdict** — green check, question mark, or cross
- the **historical basis** for it, over three seasons
- a **Narrative Street flag** where one applies
- **weather flags** scoped to the positions each condition actually affects

## On "positive feedback from other analysts"

There is no free analyst-consensus source. What exists is Sleeper's trending
adds — the number of fantasy managers who added a player in the last 24 hours.
That is crowd behaviour, not analyst opinion, and it is labelled as such
wherever it appears. Sleeper's own projections endpoint is wired but returns
empty for unplayed weeks.

A prediction with no historical basis is never graded green on crowd signal
alone. The best it earns is a question mark with the crowd signal noted, because
"lots of people added him" is not evidence a line will clear.
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sources"

import re
from dataclasses import dataclass, field

import config
from models import Narrative
from sources.markets import STATS, Analysis, Question, analyze, slate_for

VERDICT_GOOD = "✓"
VERDICT_UNSURE = "?"
VERDICT_BAD = "✕"
MARK_STORY = "\U0001F4E3"

# Where the estimate has to land to earn a check or a cross.
GOOD_THRESHOLD = 55.0
BAD_THRESHOLD = 45.0

LINE = re.compile(
    r"^(?P<player>.+?)\s+(?P<direction>over|under)?\s*(?P<threshold>-?\d+(?:\.\d+)?)\s+"
    r"(?P<stat>[a-z_]+)\s*$",
    re.IGNORECASE,
)


class PredictionsUnavailable(Exception):
    """Raised when a prediction file cannot be read."""


@dataclass
class Graded:
    question: Question
    analysis: Analysis
    verdict: str = VERDICT_UNSURE
    reasons: list[str] = field(default_factory=list)
    weather_flags: list[str] = field(default_factory=list)
    narrative: Narrative | None = None
    crowd: str = ""

    @property
    def markers(self) -> str:
        return (MARK_STORY + " " if self.narrative else "") + self.verdict


def parse(text: str) -> tuple[list[Question], list[str]]:
    """Read predictions, returning the parsed ones and any lines that failed."""
    questions: list[Question] = []
    bad: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        # Accept CSV as well: player,over,70.5,receiving_yards
        if "," in line:
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) >= 3 and parts[0].lower() not in {"player", "name"}:
                player = parts[0]
                direction = parts[1].lower() if parts[1].lower() in {"over", "under"} else "over"
                rest = [p for p in parts[1:] if p.lower() not in {"over", "under"}]
                try:
                    threshold = float(rest[0])
                except (ValueError, IndexError):
                    bad.append(line)
                    continue
                stat = rest[1] if len(rest) > 1 else ""
                if stat in STATS:
                    questions.append(Question(player, stat, threshold, direction == "over"))
                    continue
            bad.append(line)
            continue

        match = LINE.match(line)
        if not match:
            bad.append(line)
            continue

        stat = match.group("stat").lower()
        if stat not in STATS:
            bad.append(f"{line}   (unknown stat {stat!r})")
            continue

        questions.append(
            Question(
                match.group("player").strip(),
                stat,
                float(match.group("threshold")),
                (match.group("direction") or "over").lower() == "over",
            )
        )

    return questions, bad


def _crowd_signal(player_name: str) -> str:
    """Sleeper trending adds. Crowd behaviour, not analyst opinion."""
    try:
        import requests

        from sources.odds import strict_name

        trending = requests.get(
            "https://api.sleeper.app/v1/players/nfl/trending/add",
            params={"lookback_hours": 24, "limit": 50},
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        trending.raise_for_status()
        rows = trending.json()

        players = requests.get(
            "https://api.sleeper.app/v1/players/nfl",
            timeout=config.REQUEST_TIMEOUT_SECONDS * 4,
        )
        players.raise_for_status()
        catalogue = players.json()
    except Exception:
        return ""

    key = strict_name(player_name)
    for rank, row in enumerate(rows, 1):
        entry = catalogue.get(str(row.get("player_id")), {})
        name = entry.get("full_name") or ""
        if name and strict_name(name) == key:
            return (
                f"#{rank} in Sleeper adds over 24h ({row.get('count', 0):,} managers) "
                "— crowd behaviour, not analyst opinion"
            )
    return ""


def grade(question: Question, use_conditions: bool = True) -> Graded:
    """Grade one prediction."""
    slate = slate_for(question.player_name) if use_conditions else None
    analysis = analyze(question, slate=slate)
    result = Graded(question=question, analysis=analysis)

    # --- weather, scoped to the positions each condition affects ---
    if slate is not None and slate.weather is not None and not slate.weather.is_indoor:
        position = slate.player.position.value
        weather = slate.weather
        if position in config.WIND_AFFECTED_POSITIONS:
            if weather.wind_mph >= config.WIND_WARNING_MPH:
                result.weather_flags.append(
                    f"{weather.wind_mph:.0f} mph wind — degrades the "
                    f"{'throwing' if position == 'QB' else 'kicking'} game"
                )
        if position in config.RAIN_AFFECTED_POSITIONS:
            if weather.precipitation_chance >= config.PRECIP_WARNING_CHANCE:
                result.weather_flags.append(
                    f"{weather.precipitation_chance:.0%} chance of rain — ball "
                    f"security and footing for {position}s"
                )
        if weather.temperature_f <= config.COLD_WARNING_F:
            result.weather_flags.append(f"{weather.temperature_f:.0f}°F at kickoff")

    result.narrative = analysis.narrative
    result.crowd = _crowd_signal(question.player_name)

    # --- verdict ---
    if not analysis.games:
        # No history. Crowd interest is not evidence, so this never grades green.
        result.verdict = VERDICT_UNSURE
        result.reasons.append("no games found in the last three published seasons")
        if result.crowd:
            result.reasons.append(result.crowd)
        else:
            result.reasons.append("no corroborating signal available either")
        return result

    estimate = analysis.estimate
    if analysis.games < 6:
        result.verdict = VERDICT_UNSURE
        result.reasons.append(
            f"only {analysis.games} games of history — too few to grade"
        )
    elif estimate >= GOOD_THRESHOLD:
        result.verdict = VERDICT_GOOD
        result.reasons.append(f"{estimate:.0f}% estimated, above the {GOOD_THRESHOLD:.0f}% bar")
    elif estimate <= BAD_THRESHOLD:
        result.verdict = VERDICT_BAD
        result.reasons.append(f"{estimate:.0f}% estimated, below the {BAD_THRESHOLD:.0f}% bar")
    else:
        result.verdict = VERDICT_UNSURE
        result.reasons.append(f"{estimate:.0f}% estimated — inside the coin-flip band")

    low, high = analysis.interval
    if high - low > 40:
        result.reasons.append(
            f"interval {low:.0f}–{high:.0f}% is wider than most edges"
        )

    for adjustment in analysis.adjustments:
        result.reasons.append(f"{adjustment.delta:+.0f} pts — {adjustment.reason}")

    return result


def grade_all(questions: list[Question], use_conditions: bool = True) -> list[Graded]:
    return [grade(q, use_conditions) for q in questions]


# --- Output -----------------------------------------------------------------


def format_graded(results: list[Graded], bad: list[str]) -> str:
    lines = ["Graded predictions", "=" * 72, ""]

    for result in results:
        analysis = result.analysis
        low, high = analysis.interval
        basis = (
            f"{analysis.hits}/{analysis.games} games, {analysis.base_rate:.0f}% "
            f"base ({low:.0f}–{high:.0f}%)"
            if analysis.games
            else "no history"
        )
        lines.append(f"  {result.markers:<4} {result.question}")
        estimate = (
            f"estimate {analysis.estimate:.0f}%" if analysis.has_basis else "no estimate"
        )
        lines.append(f"       {basis}   {estimate}")
        for reason in result.reasons:
            lines.append(f"         · {reason}")
        for flag in result.weather_flags:
            lines.append(f"         ⛆ {flag}")
        if result.narrative:
            n = result.narrative
            lines.append(f"         {MARK_STORY} [{n.strength.value}] {n.headline}")
            lines.append(f"           {n.detail}")
        if result.crowd and analysis.games:
            lines.append(f"         · {result.crowd}")
        lines.append("")

    if bad:
        lines.append("Could not parse:")
        lines.extend(f"  {line}" for line in bad)
        lines.append("")

    good = sum(1 for r in results if r.verdict == VERDICT_GOOD)
    unsure = sum(1 for r in results if r.verdict == VERDICT_UNSURE)
    poor = sum(1 for r in results if r.verdict == VERDICT_BAD)
    lines.append(
        f"{VERDICT_GOOD} {good} favourable   {VERDICT_UNSURE} {unsure} unclear   "
        f"{VERDICT_BAD} {poor} unfavourable"
    )
    lines.append("")
    lines.append(
        "Grades come from historical base rates adjusted for observable "
        "conditions. They are not forecasts and carry no information the market "
        "lacks."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Grade Kalshi/Polymarket positions.")
    parser.add_argument("file", help="file of predictions, one per line")
    parser.add_argument(
        "--no-conditions",
        action="store_true",
        help="history only — skip weather, injuries, and narrative",
    )
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.exists():
        print(f"No such file: {path}")
        return 1

    questions, bad = parse(path.read_text(encoding="utf-8"))
    if not questions:
        print("No predictions parsed. See predictions/README.md for the format.")
        for line in bad:
            print(f"  could not parse: {line}")
        return 1

    results = grade_all(questions, use_conditions=not args.no_conditions)
    print(format_graded(results, bad))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
