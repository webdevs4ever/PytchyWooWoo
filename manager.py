"""Scoring-rule versioning and the dashboard view model.

The Manager role from ARCHITECTURE.md, implemented as a plain module. It owns
two things that change for reasons outside this codebase:

**Game rules.** DraftKings and FanDuel revise scoring between seasons. A single
hardcoded `ScoringRules` silently misvalues every historical comparison the day
a platform changes something, and leaves no record of what changed or when. Rules
are therefore versioned with an effective date, and looked up by the date being
scored rather than assumed current.

**The view model.** The dashboard renders cards, not `PlayerSlate`s. Building
that shape here keeps presentation decisions out of the rules engine and gives
any future UI — web, CLI table, JSON export — one contract to consume.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone

import config
import qa
import rules
from models import Flag, Platform, PlayerSlate, ScoringRules, Severity

# --- Rule versioning --------------------------------------------------------


@dataclass(frozen=True)
class RuleSetVersion:
    """One platform's scoring rules, valid from a date until superseded."""

    platform: Platform
    effective_from: date
    rules: ScoringRules
    note: str = ""


# Ordered oldest to newest per platform. Add a new entry when a platform changes
# scoring; never edit an existing one, or historical scoring becomes unreproducible.
RULE_HISTORY: list[RuleSetVersion] = [
    RuleSetVersion(
        platform=Platform.DRAFTKINGS,
        effective_from=date(2014, 1, 1),
        rules=config.DK_SCORING,
        note="NFL Classic: full PPR, +3 bonus at 100 rush/rec yards and 300 pass yards",
    ),
    RuleSetVersion(
        platform=Platform.FANDUEL,
        effective_from=date(2018, 1, 1),
        rules=config.FD_SCORING,
        note="Half PPR, no yardage bonuses, -2 per lost fumble",
    ),
]


class RulesUnavailable(Exception):
    """Raised when no rule set covers the requested platform and date."""


def active_rules(platform: Platform, on: date | None = None) -> ScoringRules:
    """The rule set in force for `platform` on `on` (default today)."""
    on = on or datetime.now(timezone.utc).date()
    candidates = [
        version
        for version in RULE_HISTORY
        if version.platform is platform and version.effective_from <= on
    ]
    if not candidates:
        raise RulesUnavailable(f"No {platform.value} rules effective on {on}")
    return max(candidates, key=lambda v: v.effective_from).rules


def validate_rule_sets() -> list[qa.Issue]:
    """Sanity-check the rule history. Reuses the QA issue vocabulary."""
    issues: list[qa.Issue] = []

    for platform in Platform:
        versions = [v for v in RULE_HISTORY if v.platform is platform]
        if not versions:
            issues.append(
                qa.Issue(
                    qa.IssueLevel.ERROR,
                    "rules.missing_platform",
                    f"no scoring rules defined for {platform.value}",
                    "manager",
                )
            )
            continue

        dates = [v.effective_from for v in versions]
        if len(dates) != len(set(dates)):
            issues.append(
                qa.Issue(
                    qa.IssueLevel.ERROR,
                    "rules.duplicate_effective_date",
                    f"{platform.value} has two rule sets with the same effective date — "
                    "lookup is ambiguous",
                    "manager",
                )
            )

        current = max(versions, key=lambda v: v.effective_from)
        if current.rules.salary_cap <= 0:
            issues.append(
                qa.Issue(
                    qa.IssueLevel.ERROR,
                    "rules.invalid_cap",
                    f"{platform.value} salary cap is {current.rules.salary_cap}",
                    "manager",
                )
            )

        age = (datetime.now(timezone.utc).date() - current.effective_from).days // 365
        if age >= 3:
            issues.append(
                qa.Issue(
                    qa.IssueLevel.NOTICE,
                    "rules.unreviewed",
                    f"{platform.value} rules unchanged for {age} years — "
                    "confirm the platform has not revised scoring",
                    "manager",
                )
            )

    return issues


# --- View model -------------------------------------------------------------

# Card accent by worst flag severity, matching the mockup's palette.
SEVERITY_ACCENT = {
    Severity.CRITICAL: "#f0522c",
    Severity.WARNING: "#e8a33d",
    Severity.INFO: "#4a7ec8",
}
NEUTRAL_ACCENT = "#3f4c8c"


@dataclass
class PlayerCard:
    """One dashboard card. Mirrors the mockup: avatar initials, name, number."""

    player_id: str
    name: str
    initials: str
    number: str
    position: str
    team: str
    matchup: str
    accent: str
    conditions: str = ""
    flags: list[dict] = field(default_factory=list)
    pricing: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    playable: bool = True


def _worst_severity(flags: list[Flag]) -> Severity | None:
    for severity in (Severity.CRITICAL, Severity.WARNING, Severity.INFO):
        if any(flag.severity is severity for flag in flags):
            return severity
    return None


def build_card(slate: PlayerSlate, issues: list[qa.Issue] | None = None) -> PlayerCard:
    """Turn one slate into a render-ready card."""
    issues = issues or []
    flags = rules.evaluate(slate)
    severity = _worst_severity(flags)

    conditions = ""
    if slate.weather:
        w = slate.weather
        conditions = (
            "Indoor"
            if w.is_indoor
            else f"{w.temperature_f:.0f}°F · {w.wind_mph:.0f} mph · "
            f"{w.precipitation_chance:.0%} precip"
        )

    mine = [i for i in issues if i.subject == slate.player.name]

    return PlayerCard(
        player_id=slate.player.player_id,
        name=slate.player.name,
        initials=slate.player.initials,
        number=f"#{slate.player.number}" if slate.player.number else slate.player.position.value,
        position=slate.player.position.value,
        team=slate.player.team,
        matchup=str(slate.game),
        accent=SEVERITY_ACCENT.get(severity, NEUTRAL_ACCENT) if severity else NEUTRAL_ACCENT,
        conditions=conditions,
        flags=[
            {"code": f.code, "severity": f.severity.value, "reason": f.reason} for f in flags
        ],
        pricing=[
            {
                "platform": platform.value,
                "salary": pricing.salary,
                "projected": pricing.projected_points,
                "value": round(pricing.points_per_thousand, 2),
            }
            for platform, pricing in sorted(slate.pricing.items(), key=lambda kv: kv[0].value)
        ],
        issues=[
            {"level": i.level.value, "code": i.code, "message": i.message} for i in mine
        ],
        # A player ruled out should not render as selectable.
        playable=not any(i.code == "injury.out" for i in mine),
    )


def build_view(slates: list[PlayerSlate], run_qa: bool = True) -> dict:
    """The full dashboard payload: cards plus run-level metadata."""
    report = qa.validate(slates) if run_qa else qa.QAReport([])

    cards = [build_card(slate, report.issues) for slate in slates]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "splits_season": config.SPLITS_SEASON,
        "scoring": {
            platform.value: {
                "salary_cap": active_rules(platform).salary_cap,
                "points_per_reception": active_rules(platform).points_per_reception,
            }
            for platform in Platform
        },
        "quality": {
            "ok": report.ok,
            "errors": len(report.errors),
            "warnings": len(report.warnings),
            "notices": len(report.notices),
        },
        "cards": [asdict(card) for card in cards],
    }


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point: validate rules, then emit the view model as JSON."""
    import argparse

    parser = argparse.ArgumentParser(description="Scoring rules and dashboard view model.")
    parser.add_argument("--rules", action="store_true", help="validate rule sets and exit")
    parser.add_argument("--indent", type=int, default=2, help="JSON indent")
    args = parser.parse_args(argv)

    rule_issues = validate_rule_sets()
    if args.rules:
        if not rule_issues:
            print("Rules: no issues.")
        for issue in rule_issues:
            print(issue)
        return 0 if not any(i.level is qa.IssueLevel.ERROR for i in rule_issues) else 1

    import main as pipeline

    slates = pipeline.build_demo_slates()
    pipeline.enrich_with_weather(slates, quiet=True)
    pipeline.enrich_with_splits(slates, quiet=True)
    pipeline.enrich_with_pricing(slates, quiet=True)

    print(json.dumps(build_view(slates), indent=args.indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
