"""Schedule ingestion, from nflverse.

Supplies who plays whom, which everything downstream needs and nothing else
provided. Narrative detection in particular cannot run league-wide without it —
a revenge game is a player and an opponent, and the opponent has to come from
somewhere.

The file also carries `roof`, which is better than a hardcoded dome list: it
reflects the venue actually used, including neutral-site and international
games.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import requests

import config
from models import Game, Venue

SCHEDULE_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
)

# nflverse writes the Rams as LA in the schedule and LAR in rosters.
TEAM_ALIASES = {"LA": "LAR"}

# `roof` values that mean the forecast is irrelevant.
INDOOR_ROOFS = {"dome", "closed"}


class ScheduleUnavailable(Exception):
    """Raised when the schedule cannot be retrieved."""


def normalize_team(team: str) -> str:
    code = (team or "").strip().upper()
    return TEAM_ALIASES.get(code, code)


def _download() -> str:
    cached = config.CACHE_DIR / "games.csv"
    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_text(encoding="utf-8")

    try:
        resp = requests.get(SCHEDULE_URL, timeout=config.REQUEST_TIMEOUT_SECONDS * 4)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ScheduleUnavailable(f"Could not download schedule: {exc}") from exc

    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(resp.text, encoding="utf-8")
    return resp.text


def _kickoff(row: dict) -> datetime:
    """Parse gameday plus gametime, falling back to midnight UTC."""
    day = (row.get("gameday") or "").strip()
    time_text = (row.get("gametime") or "").strip()
    for fmt, text in (("%Y-%m-%d %H:%M", f"{day} {time_text}"), ("%Y-%m-%d", day)):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _venue(row: dict, home: str) -> Venue:
    """Prefer the schedule's own roof and stadium over the static table."""
    fallback = config.venue_for(home)
    roof = (row.get("roof") or "").strip().lower()
    name = (row.get("stadium") or "").strip() or (fallback.name if fallback else home)

    if fallback is None:
        # An unknown home venue still yields a usable game; weather will simply
        # be skipped for it rather than the whole slate failing.
        return Venue(name, 0.0, 0.0, is_dome=roof in INDOOR_ROOFS)

    return Venue(name, fallback.latitude, fallback.longitude, is_dome=roof in INDOOR_ROOFS)


def games_for(season: int, week: int) -> list[Game]:
    """Every game in one week."""
    rows = csv.DictReader(io.StringIO(_download()))
    out: list[Game] = []

    for row in rows:
        try:
            if int(row.get("season") or 0) != season or int(row.get("week") or 0) != week:
                continue
        except ValueError:
            continue

        home = normalize_team(row.get("home_team", ""))
        away = normalize_team(row.get("away_team", ""))
        if not home or not away:
            continue

        out.append(
            Game(
                game_id=row.get("game_id") or f"{season}-{week}-{away}-{home}",
                home_team=home,
                away_team=away,
                kickoff=_kickoff(row),
                venue=_venue(row, home),
            )
        )

    if not out:
        raise ScheduleUnavailable(f"No games found for {season} week {week}")
    return out


def current_week(season: int | None = None) -> tuple[int, int]:
    """The season and week whose games are nearest to now.

    Picks the earliest week with a game still ahead, so a mid-week run reports
    the upcoming slate rather than the one just played.
    """
    rows = list(csv.DictReader(io.StringIO(_download())))
    now = datetime.now(timezone.utc)

    upcoming = []
    for row in rows:
        try:
            row_season, week = int(row.get("season") or 0), int(row.get("week") or 0)
        except ValueError:
            continue
        if season is not None and row_season != season:
            continue
        if _kickoff(row) >= now:
            upcoming.append((row_season, week))

    if upcoming:
        return min(upcoming)

    played = []
    for row in rows:
        try:
            played.append((int(row.get("season") or 0), int(row.get("week") or 0)))
        except ValueError:
            continue
    if not played:
        raise ScheduleUnavailable("Schedule contains no usable rows")
    return max(played)
