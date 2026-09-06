"""Roster ingestion, from nflverse.

Supplies jersey numbers for the dashboard cards, and roster status for QA.

NFL.com was considered and rejected: it publishes no developer API, so using it
would mean scraping — brittle, against their terms, and the only such dependency
in an otherwise clean list. nflverse already carries the same fields under the
same free, no-auth terms as the splits and injury feeds.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import requests

import config
from models import RosterEntry


class RostersUnavailable(Exception):
    """Raised when roster data cannot be retrieved."""


def _season_url(season: int) -> str:
    return f"{config.NFLVERSE_ROSTER_URL}/roster_{season}.csv"


def _download_season(season: int) -> str:
    cached = config.CACHE_DIR / f"roster_{season}.csv"
    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_text(encoding="utf-8")

    try:
        resp = requests.get(
            _season_url(season), timeout=config.REQUEST_TIMEOUT_SECONDS * 4
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RostersUnavailable(f"Could not download {season} roster: {exc}") from exc

    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(resp.text, encoding="utf-8")
    return resp.text


def resolve_season(preferred: int | None = None) -> int:
    """Most recent season with a published roster file."""
    start = preferred or config.ROSTER_SEASON or date.today().year
    for season in range(start, start - 3, -1):
        cached = config.CACHE_DIR / f"roster_{season}.csv"
        if cached.exists() and cached.stat().st_size > 0:
            return season
        try:
            resp = requests.head(
                _season_url(season),
                allow_redirects=True,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            if resp.status_code == 200:
                return season
        except requests.RequestException:
            continue
    raise RostersUnavailable(f"No roster published for {start} or the 2 prior")


class RosterBook:
    """One season's rosters, indexed by normalized name.

    Indexed by strict full name, because the loose first-initial key collides
    badly here — four players in the 2026 roster reduce to 'a.brown', two of
    them on Detroit. The loose key is kept only as a last-resort fallback, and
    only when it resolves to exactly one player.
    """

    def __init__(self, season: int | None = None) -> None:
        self.season = resolve_season(season)
        self._by_strict: dict[str, list[RosterEntry]] = {}
        self._by_name: dict[str, list[RosterEntry]] = {}
        self._build()

    def _build(self) -> None:
        from sources.odds import normalize_name, strict_name

        text = _download_season(self.season)
        reader = csv.DictReader(io.StringIO(text))

        for row in reader:
            key = normalize_name(row.get("full_name", ""))
            if not key:
                continue

            raw_number = (row.get("jersey_number") or "").strip()
            try:
                number = int(float(raw_number)) if raw_number else None
            except ValueError:
                number = None

            entry = RosterEntry(
                player_name=row.get("full_name", ""),
                team=(row.get("team") or "").strip().upper(),
                position=(row.get("position") or "").strip(),
                jersey_number=number,
                status=(row.get("status") or "").strip(),
            )
            self._by_strict.setdefault(strict_name(row.get("full_name", "")), []).append(entry)
            self._by_name.setdefault(key, []).append(entry)

    def entry_for(self, player_name: str, team: str | None = None) -> RosterEntry | None:
        """Resolve a player, preferring an exact full-name match.

        Returns None rather than guessing when a name is ambiguous — a wrong
        jersey number renders confidently and is worse than a missing one.
        """
        from sources.odds import normalize_name, strict_name

        wanted_team = team.strip().upper() if team else None

        def pick(candidates: list[RosterEntry]) -> RosterEntry | None:
            if wanted_team:
                on_team = [e for e in candidates if e.team == wanted_team]
                if len(on_team) == 1:
                    return on_team[0]
                if len(on_team) > 1:
                    return None  # genuinely ambiguous
            return candidates[0] if len(candidates) == 1 else None

        exact = pick(self._by_strict.get(strict_name(player_name), []))
        if exact:
            return exact

        return pick(self._by_name.get(normalize_name(player_name), []))

    def number_for(self, player_name: str, team: str | None = None) -> int | None:
        entry = self.entry_for(player_name, team)
        return entry.jersey_number if entry else None

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_strict.values())


_BOOK: RosterBook | None = None


def get_book(season: int | None = None) -> RosterBook:
    global _BOOK
    if _BOOK is None:
        _BOOK = RosterBook(season)
    return _BOOK


def fetch_entry(player_name: str, team: str | None = None) -> RosterEntry | None:
    """Return a player's roster record, or None if not found or ambiguous."""
    return get_book().entry_for(player_name, team)
