"""DraftKings / FanDuel pricing ingestion.

## A correction to the handoff's premise

`handoff2.md` treats "DK/FanDuel odds & pricing" as one data need served by one
aggregator. They are two different things from two different products:

- **DraftKings Sportsbook** publishes betting odds. Aggregators like The Odds
  API license these, and player props (passing yards, receptions) are a usable
  basis for *projections*.
- **DraftKings DFS** publishes contest salaries and salary caps. No aggregator
  carries these, because they are not odds.

So projections and salaries come from separate paths here:

1. **Projections** — player props via The Odds API, converted to a `StatLine`
   and then scored under each platform's own rules.
2. **Salaries** — the contest CSV that DraftKings and FanDuel both let a logged-in
   user download from the lineup page. User-initiated, no scraping, no ToS
   problem. Drop the files in `config.SALARY_DIR`.

The salary export also carries a season average (DK `AvgPointsPerGame`, FD
`FPPG`), so this module produces useful pricing with no API key at all — props
refine the projection when a key is present.
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

import config
from models import Platform, Player, Position, Pricing, StatLine

# Odds API market -> StatLine field.
MARKET_TO_FIELD = {
    "player_pass_yds": "passing_yards",
    "player_pass_tds": "passing_tds",
    "player_rush_yds": "rushing_yards",
    "player_reception_yds": "receiving_yards",
    "player_receptions": "receptions",
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


class OddsUnavailable(Exception):
    """Raised when pricing cannot be retrieved."""


def normalize_name(name: str) -> str:
    """Reduce a player name to a join key.

    Sources disagree on format: nflverse writes 'P.Mahomes', salary exports
    write 'Patrick Mahomes', props write 'Patrick Mahomes II'. Collapsing to
    first-initial plus last name matches across all three.
    """
    cleaned = re.sub(r"[^a-zA-Z.\s'-]", " ", name or "").strip()
    if not cleaned:
        return ""

    parts = [p for p in re.split(r"[\s.]+", cleaned) if p]
    parts = [p for p in parts if p.lower().strip("'-") not in _SUFFIXES]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].lower()

    return f"{parts[0][0].lower()}.{parts[-1].lower()}"


def strict_name(name: str) -> str:
    """A full-name key, for sources that carry complete names on both sides.

    `normalize_name` collapses to first-initial-plus-last, which is necessary to
    join nflverse's 'P.Mahomes' to a salary export's 'Patrick Mahomes' — but it
    is far too lossy where full names are available. Four players in the 2026
    roster reduce to 'a.brown', two of them on Detroit. Use this instead
    wherever both sides have full names.
    """
    cleaned = re.sub(r"[^a-zA-Z\s]", "", name or "").lower()
    parts = [p for p in cleaned.split() if p and p not in _SUFFIXES]
    return "".join(parts)


# --- Salary exports ---------------------------------------------------------


def _detect_platform(fieldnames: list[str]) -> Platform | None:
    """DK and FD export different column sets; identify which one this is."""
    fields = {f.strip() for f in fieldnames}
    if "Name + ID" in fields or "AvgPointsPerGame" in fields:
        return Platform.DRAFTKINGS
    if "Nickname" in fields or "FPPG" in fields:
        return Platform.FANDUEL
    return None


@dataclass(frozen=True)
class Candidate:
    """A priced, position-tagged player from a contest export.

    The optimizer draws from the whole export, not just the slate, so position
    and team have to survive parsing — `Pricing` alone is not enough to build a
    lineup from.
    """

    player: Player
    pricing: Pricing


def load_salaries(
    path: Path,
    platform: Platform | None = None,
    pool: list[Candidate] | None = None,
) -> dict[str, Pricing]:
    """Parse a DraftKings or FanDuel contest export into `Pricing` by name key.

    The export's season average is used as the baseline projection; props
    override it later when available. When `pool` is given, full candidates are
    appended to it for the optimizer.
    """
    if not path.exists():
        raise OddsUnavailable(f"Salary export not found: {path}")

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        platform = platform or _detect_platform(fieldnames)
        if platform is None:
            raise OddsUnavailable(
                f"Could not identify platform from columns in {path.name}: {fieldnames}"
            )

        out: dict[str, Pricing] = {}
        for row in reader:
            if platform is Platform.DRAFTKINGS:
                name = row.get("Name") or ""
                raw_salary = row.get("Salary")
                raw_projection = row.get("AvgPointsPerGame")
                raw_position = row.get("Position") or ""
                team = (row.get("TeamAbbrev") or "").strip().upper()
            else:
                name = (
                    row.get("Nickname")
                    or f"{row.get('First Name', '')} {row.get('Last Name', '')}"
                )
                raw_salary = row.get("Salary")
                raw_projection = row.get("FPPG")
                raw_position = row.get("Position") or ""
                team = (row.get("Team") or "").strip().upper()

            key = normalize_name(name)
            if not key:
                continue

            try:
                salary = int(float(raw_salary or 0))
                projection = float(raw_projection or 0.0)
            except ValueError:
                continue
            if salary <= 0:
                continue

            price = Pricing(
                platform=platform, salary=salary, projected_points=projection
            )
            out[key] = price

            if pool is not None:
                try:
                    position = Position(raw_position.strip().upper())
                except ValueError:
                    continue  # a slot label like RB/FLEX, or an unknown position
                pool.append(
                    Candidate(
                        player=Player(key, name, position, team),
                        pricing=price,
                    )
                )

    if not out:
        raise OddsUnavailable(f"No usable rows in {path.name}")
    return out


def load_all_salaries(
    directory: Path | None = None, pools: dict[Platform, list[Candidate]] | None = None
) -> dict[Platform, dict[str, Pricing]]:
    """Load every salary export in `directory`, keyed by platform.

    Missing files are not an error — the caller degrades to whatever is present.
    """
    directory = directory or config.SALARY_DIR
    tables: dict[Platform, dict[str, Pricing]] = {}
    if not directory.exists():
        return tables

    for path in sorted(directory.glob("*.csv")):
        collected: list[Candidate] = []
        try:
            table = load_salaries(path, pool=collected)
        except OddsUnavailable:
            continue
        if table:
            platform = next(iter(table.values())).platform
            tables.setdefault(platform, {}).update(table)
            if pools is not None and collected:
                pools.setdefault(platform, []).extend(collected)
    return tables


# --- Player props -----------------------------------------------------------


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", key)
    return config.CACHE_DIR / f"odds_{safe}.json"


def _cache_read(key: str) -> object | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > config.ODDS_CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _cache_write(key: str, payload: object) -> None:
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _get(url: str, params: dict, cache_key: str) -> object:
    """GET with a disk cache, because the free tier allows ~500 calls a month."""
    cached = _cache_read(cache_key)
    if cached is not None:
        return cached

    if not config.ODDS_API_KEY:
        raise OddsUnavailable(
            "ODDS_API_KEY is not set — props unavailable (salary exports still work)"
        )

    try:
        resp = requests.get(
            url,
            params={**params, "apiKey": config.ODDS_API_KEY},
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise OddsUnavailable(f"Odds API request failed: {exc}") from exc

    payload = resp.json()
    _cache_write(cache_key, payload)
    return payload


def list_events() -> list[dict]:
    """Upcoming NFL events, used to resolve prop requests per game."""
    url = f"{config.ODDS_API_BASE_URL}/sports/{config.ODDS_API_SPORT}/events"
    payload = _get(url, {}, "events")
    return payload if isinstance(payload, list) else []


def fetch_event_props(event_id: str) -> dict[Platform, dict[str, StatLine]]:
    """Player props for one game, as a projected `StatLine` per player.

    Each market's 'Over' line is treated as the projection — the number the book
    considers a coin flip is the closest thing to a consensus expectation.
    """
    url = (
        f"{config.ODDS_API_BASE_URL}/sports/{config.ODDS_API_SPORT}"
        f"/events/{event_id}/odds"
    )
    payload = _get(
        url,
        {
            "regions": "us",
            "bookmakers": "draftkings,fanduel",
            "markets": ",".join(config.ODDS_API_MARKETS),
            "oddsFormat": "american",
        },
        f"props_{event_id}",
    )

    bookmaker_to_platform = {
        "draftkings": Platform.DRAFTKINGS,
        "fanduel": Platform.FANDUEL,
    }
    accumulated: dict[Platform, dict[str, dict[str, float]]] = {}

    for bookmaker in (payload or {}).get("bookmakers", []):
        platform = bookmaker_to_platform.get(bookmaker.get("key"))
        if platform is None:
            continue

        for market in bookmaker.get("markets", []):
            field = MARKET_TO_FIELD.get(market.get("key"))
            if field is None:
                continue

            for outcome in market.get("outcomes", []):
                if outcome.get("name") != "Over":
                    continue
                key = normalize_name(outcome.get("description", ""))
                point = outcome.get("point")
                if not key or point is None:
                    continue
                accumulated.setdefault(platform, {}).setdefault(key, {})[field] = float(
                    point
                )

    return {
        platform: {key: StatLine(**fields) for key, fields in players.items()}
        for platform, players in accumulated.items()
    }


# --- Assembly ---------------------------------------------------------------


class PricingBook:
    """Combined salaries and projections, loaded once and queried per player."""

    def __init__(self, use_props: bool = True) -> None:
        self.pools: dict[Platform, list[Candidate]] = {}
        self.salaries = load_all_salaries(pools=self.pools)
        self.props: dict[Platform, dict[str, StatLine]] = {}
        self.props_error: str | None = None

        if use_props:
            try:
                for event in list_events():
                    for platform, players in fetch_event_props(event["id"]).items():
                        self.props.setdefault(platform, {}).update(players)
            except (OddsUnavailable, KeyError) as exc:
                self.props_error = str(exc)

    def pool_for(self, platform: Platform) -> list[Candidate]:
        """Every priced player on one platform, with projections applied.

        Manual corrections are layered last and win outright — including
        players the export omits entirely, which are appended to the pool.
        """
        import overrides as ov

        try:
            corrections = ov.load()
        except ov.OverrideError:
            corrections = ov.Overrides()

        out: list[Candidate] = []
        props = self.props.get(platform, {})
        seen: set[str] = set()

        for candidate in self.pools.get(platform, []):
            key = normalize_name(candidate.player.name)
            stats = props.get(key)
            if stats is not None and not stats.is_empty():
                projected = config.SCORING[platform].score(stats)
                out.append(
                    Candidate(
                        player=candidate.player,
                        pricing=Pricing(platform, candidate.pricing.salary, projected),
                    )
                )
            else:
                out.append(candidate)

        # Layer corrections over what the export gave us.
        adjusted: list[Candidate] = []
        for candidate in out:
            key = strict_name(candidate.player.name)
            seen.add(key)
            override = corrections.get(key)
            if override is None or not override.has_pricing():
                adjusted.append(candidate)
                continue
            adjusted.append(
                Candidate(
                    player=candidate.player,
                    pricing=Pricing(
                        platform,
                        override.salary
                        if override.salary is not None
                        else candidate.pricing.salary,
                        override.projected_points
                        if override.projected_points is not None
                        else candidate.pricing.projected_points,
                    ),
                )
            )

        # A correction can also supply a player the export omits entirely.
        for key, override in corrections.players.items():
            if key in seen or override.salary is None:
                continue
            try:
                position = Position((override.position or "").upper())
            except ValueError:
                continue
            adjusted.append(
                Candidate(
                    player=Player(
                        key, override.display_name or key, position, override.team or ""
                    ),
                    pricing=Pricing(
                        platform, override.salary, override.projected_points or 0.0
                    ),
                )
            )

        return adjusted

    def pricing_for(self, player: Player) -> dict[Platform, Pricing]:
        """Salary and projection per platform, empty when nothing is known."""
        key = normalize_name(player.name)
        out: dict[Platform, Pricing] = {}

        for platform, table in self.salaries.items():
            base = table.get(key)
            if base is None:
                continue

            stats = self.props.get(platform, {}).get(key)
            if stats is not None and not stats.is_empty():
                projected = config.SCORING[platform].score(stats)
            else:
                projected = base.projected_points

            out[platform] = Pricing(
                platform=platform, salary=base.salary, projected_points=projected
            )

        return out


_BOOK: PricingBook | None = None


def get_book(use_props: bool = True) -> PricingBook:
    global _BOOK
    if _BOOK is None:
        _BOOK = PricingBook(use_props=use_props)
    return _BOOK


def fetch_pricing(player: Player, week: int | None = None) -> dict[Platform, Pricing]:
    """Return this player's salary and projection on each platform.

    Missing platforms are simply absent from the dict; callers must not assume
    both are present.
    """
    return get_book().pricing_for(player)
