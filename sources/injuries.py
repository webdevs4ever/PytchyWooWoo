"""Official injury report ingestion, from nflverse.

nflverse publishes the weekly injury report as one CSV per season, carrying both
the game-status designation ("Out", "Doubtful", "Questionable") and the practice
participation report.

This exists for the QA layer rather than the rules engine: an injured player is
a data-quality problem — a lineup slot that will score zero — not a betting
signal to weigh.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import requests

import config
from models import InjuryStatus
from sources.splits import SplitsUnavailable  # shared cache/download conventions


class InjuriesUnavailable(Exception):
    """Raised when the injury report cannot be retrieved."""


def _season_url(season: int) -> str:
    return f"{config.NFLVERSE_INJURY_URL}/injuries_{season}.csv"


def _download_season(season: int) -> str:
    """Fetch a season's injury report, caching it on disk."""
    cached = config.CACHE_DIR / f"injuries_{season}.csv"
    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_text(encoding="utf-8")

    try:
        resp = requests.get(
            _season_url(season), timeout=config.REQUEST_TIMEOUT_SECONDS * 4
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise InjuriesUnavailable(
            f"Could not download {season} injury report: {exc}"
        ) from exc

    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(resp.text, encoding="utf-8")
    return resp.text


def resolve_season(preferred: int | None = None) -> int:
    """Return the most recent season with a published injury report.

    Walks back from the preferred season, because a new season's file does not
    appear until the first report is filed.
    """
    start = preferred or config.INJURY_SEASON or date.today().year
    for season in range(start, start - 3, -1):
        cached = config.CACHE_DIR / f"injuries_{season}.csv"
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
    raise InjuriesUnavailable(f"No injury report published for {start} or the 2 prior")


class InjuryReport:
    """One season's injury report, indexed for lookup by name and team."""

    def __init__(self, season: int | None = None) -> None:
        self.season = resolve_season(season)
        self._by_key: dict[tuple[str, int], InjuryStatus] = {}
        self._build()

    def _build(self) -> None:
        from sources.odds import normalize_name

        text = _download_season(self.season)
        reader = csv.DictReader(io.StringIO(text))

        for row in reader:
            key_name = normalize_name(row.get("full_name", ""))
            if not key_name:
                continue
            try:
                week = int(row.get("week") or 0)
            except ValueError:
                continue

            status = InjuryStatus(
                player_name=row.get("full_name", ""),
                team=(row.get("team") or "").strip().upper(),
                week=week,
                report_status=(row.get("report_status") or "").strip(),
                primary_injury=(row.get("report_primary_injury") or "").strip(),
                practice_status=(row.get("practice_status") or "").strip(),
            )
            # Later weeks overwrite earlier ones for the same player, so the
            # most recent report wins when no week is specified.
            existing = self._by_key.get((key_name, week))
            if existing is None or week >= existing.week:
                self._by_key[(key_name, week)] = status

    def status_for(self, player_name: str, week: int | None = None) -> InjuryStatus | None:
        """Look up a player's status, defaulting to their latest report."""
        from sources.odds import normalize_name

        key_name = normalize_name(player_name)
        if not key_name:
            return None

        if week is not None:
            return self._by_key.get((key_name, week))

        matches = [
            status for (name, _), status in self._by_key.items() if name == key_name
        ]
        if not matches:
            return None
        return max(matches, key=lambda s: s.week)

    def __len__(self) -> int:
        return len(self._by_key)

    @property
    def is_sparse(self) -> bool:
        """True when the report exists but has barely been filed.

        A newly published season's file appears days before the first
        designations land. Falling back to the prior season would be worse —
        a week 18 designation says nothing about week 1 — so the honest
        response is to report the emptiness rather than hide it behind a stale
        answer or a silent None.
        """
        return len(self._by_key) < config.INJURY_SPARSE_THRESHOLD


_REPORT: InjuryReport | None = None


def get_report(season: int | None = None) -> InjuryReport:
    global _REPORT
    if _REPORT is None:
        _REPORT = InjuryReport(season)
    return _REPORT


def fetch_status(player_name: str, week: int | None = None) -> InjuryStatus | None:
    """Return a player's injury designation, or None if they aren't listed."""
    return get_report().status_for(player_name, week)
