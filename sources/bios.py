"""Player biographical data from Wikipedia.

Supplies what nflverse does not: high school, hometown, and the cities attached
to them. NFL player articles carry an infobox with `high_school`, `birth_place`,
and `college`, and those fields are what the hometown narratives are built from.

**Licensing and etiquette.** Wikipedia text is CC BY-SA; the attribution sits in
the dashboard footer and in any narrative sourced from here. The API is free and
needs no key, but it does expect a descriptive User-Agent, and it is fetched one
player at a time on demand rather than swept in bulk. Every response is cached
to disk, so a player is fetched once.

This is the only source in the project that is scraped rather than published as
a dataset — but it is a documented public API with an explicit reuse licence,
which is the distinction that mattered when NFL.com and X were rejected.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import requests

import config

API = "https://en.wikipedia.org/w/api.php"

# US states, for pulling a location out of free-text infobox values.
STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

INFOBOX_FIELDS = ("high_school", "birth_place", "college", "number", "position")


class BiosUnavailable(Exception):
    """Raised when Wikipedia cannot be reached."""


@dataclass(frozen=True)
class Bio:
    player_name: str
    high_school: str = ""
    high_school_city: str = ""
    high_school_state: str = ""
    birth_place: str = ""
    birth_state: str = ""
    college: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.high_school or self.birth_place or self.college)


def _cache_path(player_name: str):
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", player_name).strip("_").lower()
    return config.CACHE_DIR / "bios" / f"{safe}.json"


def _clean(value: str) -> str:
    """Strip wiki markup down to readable text.

    `[[Mater Dei High School (Santa Ana, California)|Mater Dei]]` -> `Mater Dei`.
    """
    text = value
    text = re.sub(r"\{\{nowrap\|", "", text)
    text = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", text)  # piped link
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)             # plain link
    text = re.sub(r"\{\{[^}]*\}\}", "", text)                   # leftover templates
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", text).strip(" ,;|")


def _location(raw: str) -> tuple[str, str]:
    """Pull (city, state abbreviation) out of an infobox location value.

    Reads the *raw* wikitext rather than the cleaned display text: a piped link
    like `[[Mater Dei High School (Santa Ana, California)|Mater Dei]]` hides the
    city behind the pipe, and cleaning it discards exactly what is needed.
    """
    state_abbr = ""
    for name, abbr in STATES.items():
        if re.search(rf"\b{re.escape(name)}\b", raw):
            state_abbr = abbr
            break
    if not state_abbr:
        return "", ""

    state_name = next(n for n, a in STATES.items() if a == state_abbr)
    match = re.search(rf"([A-Z][A-Za-z.'\- ]+),\s*{re.escape(state_name)}", raw)
    city = match.group(1).strip() if match else ""
    return city, state_abbr


def _read_cache(player_name: str) -> Bio | None:
    path = _cache_path(player_name)
    if not path.exists():
        return None
    try:
        return Bio(**json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        return None


def _write_cache(bio: Bio) -> None:
    path = _cache_path(bio.player_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bio.__dict__), encoding="utf-8")


def _request(titles: list[str]) -> dict:
    """One API call for up to `WIKIPEDIA_BATCH_SIZE` articles.

    Wikipedia rate-limits a stream of single-title requests — fetching players
    one at a time returned 429 after roughly twenty. Batching turns 535 lookups
    into a dozen calls, which is both faster and the usage the API is designed
    for.
    """
    headers = {"User-Agent": config.WIKIPEDIA_USER_AGENT}
    params = {
        "action": "query",
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "format": "json",
        "redirects": 1,
        "titles": "|".join(titles),
    }

    delay = config.WIKIPEDIA_DELAY_SECONDS
    for attempt in range(3):
        try:
            resp = requests.get(
                API, params=params, headers=headers,
                timeout=config.REQUEST_TIMEOUT_SECONDS * 2,
            )
            if resp.status_code == 429:
                time.sleep(delay * (attempt + 1) * 8)
                continue
            resp.raise_for_status()
            return resp.json().get("query", {}).get("pages", {})
        except requests.RequestException as exc:
            if attempt == 2:
                raise BiosUnavailable(f"Wikipedia request failed: {exc}") from exc
            time.sleep(delay * (attempt + 1) * 4)
    raise BiosUnavailable("Wikipedia rate-limited the request three times")


def _parse_page(page: dict, fallback_name: str) -> Bio:
    title = page.get("title", fallback_name)
    bio = Bio(player_name=title)
    revisions = page.get("revisions")
    if revisions:
        text = revisions[0].get("slots", {}).get("main", {}).get("*", "")
        # Only trust an article that is actually about a footballer.
        if "infobox" in text.lower() and "football" in text.lower()[:4000]:
            values = {}
            for field in INFOBOX_FIELDS:
                match = re.search(rf"\|\s*{field}\s*=\s*(.+)", text)
                values[field] = match.group(1).strip() if match else ""

            hs_city, hs_state = _location(values["high_school"])
            _, birth_state = _location(values["birth_place"])

            bio = Bio(
                player_name=title,
                high_school=_clean(values["high_school"]),
                high_school_city=hs_city,
                high_school_state=hs_state,
                birth_place=_clean(values["birth_place"]),
                birth_state=birth_state,
                college=_clean(values["college"]),
            )
    return bio


def fetch_bios(player_names: list[str]) -> dict[str, Bio]:
    """Look up many players, batched and cached. Keyed by the name asked for."""
    out: dict[str, Bio] = {}
    pending: list[str] = []

    for name in player_names:
        cached = _read_cache(name)
        if cached is not None:
            out[name] = cached
        else:
            pending.append(name)

    size = config.WIKIPEDIA_BATCH_SIZE
    batches = (len(pending) + size - 1) // size
    if batches > config.WIKIPEDIA_MAX_BATCHES:
        # Wikipedia throttles sustained querying even when batched. Refusing a
        # bulk sweep is better than retrying into a block: the uncached players
        # come back empty, which downstream reads as "no narrative" rather than
        # producing a wrong one.
        raise BiosUnavailable(
            f"{len(pending)} uncached players would need {batches} requests, "
            f"over the {config.WIKIPEDIA_MAX_BATCHES}-batch limit. Wikipedia is "
            "a courtesy API — look players up individually, or raise "
            "WIKIPEDIA_MAX_BATCHES and accept the wait."
        )

    for start in range(0, len(pending), size):
        chunk = pending[start : start + size]
        pages = _request(chunk)

        # Redirects mean a returned title may differ from the one requested, so
        # map results back by normalised name rather than by position.
        by_title = {}
        for page in pages.values():
            parsed = _parse_page(page, page.get("title", ""))
            by_title[_norm(parsed.player_name)] = parsed
        for name in chunk:
            parsed = by_title.get(_norm(name))
            bio = Bio(player_name=name) if parsed is None else Bio(
                player_name=name,
                high_school=parsed.high_school,
                high_school_city=parsed.high_school_city,
                high_school_state=parsed.high_school_state,
                birth_place=parsed.birth_place,
                birth_state=parsed.birth_state,
                college=parsed.college,
            )
            _write_cache(bio)
            out[name] = bio

        time.sleep(config.WIKIPEDIA_DELAY_SECONDS)

    return out


def _norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def fetch_bio(player_name: str) -> Bio:
    """Look up one player. Cached on disk; a miss is cached too."""
    return fetch_bios([player_name])[player_name]
