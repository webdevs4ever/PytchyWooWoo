"""College-to-state mapping, for the homecoming narrative.

Nobody publishes campus coordinates alongside NFL roster data, so this is
curated. It is deliberately partial: 267 distinct colleges appear among active
skill players, with a long tail of one-player programs. An unmapped school
produces no narrative rather than a guessed one — a wrong homecoming claim is
worse than a missing one.

State-level, not city-level. "Playing in the state where he went to college" is
what this data can honestly support; city-level would need campus and venue
coordinates that would have to be curated twice over.
"""

from __future__ import annotations

# Programs that actually send players to the NFL, by state.
COLLEGE_STATE: dict[str, str] = {
    "Alabama": "AL", "Auburn": "AL", "UAB": "AL", "Troy": "AL", "South Alabama": "AL",
    "Arizona": "AZ", "Arizona State": "AZ", "Northern Arizona": "AZ",
    "Arkansas": "AR", "Arkansas State": "AR",
    "California": "CA", "UCLA": "CA", "USC": "CA", "Stanford": "CA", "Fresno State": "CA",
    "San Diego State": "CA", "San Jose State": "CA", "Sacramento State": "CA",
    "Colorado": "CO", "Colorado State": "CO", "Air Force": "CO",
    "UConn": "CT", "Connecticut": "CT", "Yale": "CT",
    "Delaware": "DE",
    "Florida": "FL", "Florida State": "FL", "Miami": "FL", "Miami (FL)": "FL",
    "UCF": "FL", "Central Florida": "FL", "South Florida": "FL", "Florida Atlantic": "FL",
    "Florida International": "FL",
    "Georgia": "GA", "Georgia Tech": "GA", "Georgia State": "GA",
    "Georgia Southern": "GA", "Kennesaw State": "GA",
    "Hawaii": "HI",
    "Boise State": "ID", "Idaho": "ID",
    "Illinois": "IL", "Northwestern": "IL", "Northern Illinois": "IL", "Western Illinois": "IL",
    "Indiana": "IN", "Purdue": "IN", "Notre Dame": "IN", "Ball State": "IN",
    "Iowa": "IA", "Iowa State": "IA", "Northern Iowa": "IA",
    "Kansas": "KS", "Kansas State": "KS", "Wichita State": "KS",
    "Kentucky": "KY", "Louisville": "KY", "Western Kentucky": "KY",
    "LSU": "LA", "Tulane": "LA", "Louisiana": "LA", "Louisiana Tech": "LA",
    "Louisiana-Lafayette": "LA", "Grambling State": "LA",
    "Maine": "ME",
    "Maryland": "MD", "Navy": "MD", "Towson": "MD",
    "Boston College": "MA", "UMass": "MA", "Massachusetts": "MA", "Harvard": "MA",
    "Michigan": "MI", "Michigan State": "MI", "Central Michigan": "MI",
    "Western Michigan": "MI", "Eastern Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS", "Ole Miss": "MS", "Mississippi State": "MS",
    "Southern Miss": "MS", "Jackson State": "MS",
    "Missouri": "MO", "Missouri State": "MO",
    "Montana": "MT", "Montana State": "MT",
    "Nebraska": "NE",
    "Nevada": "NV", "UNLV": "NV",
    "New Hampshire": "NH", "Dartmouth": "NH",
    "Rutgers": "NJ", "Princeton": "NJ", "Monmouth": "NJ",
    "New Mexico": "NM", "New Mexico State": "NM",
    "Syracuse": "NY", "Buffalo": "NY", "Army": "NY", "Cornell": "NY", "Colgate": "NY",
    "North Carolina": "NC", "NC State": "NC", "Duke": "NC", "Wake Forest": "NC",
    "East Carolina": "NC", "Appalachian State": "NC", "Charlotte": "NC",
    "North Dakota State": "ND", "North Dakota": "ND",
    "Ohio State": "OH", "Cincinnati": "OH", "Toledo": "OH", "Ohio": "OH",
    "Bowling Green": "OH", "Akron": "OH", "Kent State": "OH", "Miami (OH)": "OH",
    "Oklahoma": "OK", "Oklahoma State": "OK", "Tulsa": "OK",
    "Oregon": "OR", "Oregon State": "OR", "Portland State": "OR",
    "Penn State": "PA", "Pittsburgh": "PA", "Pitt": "PA", "Temple": "PA",
    "Villanova": "PA", "Penn": "PA",
    "Clemson": "SC", "South Carolina": "SC", "Coastal Carolina": "SC", "Furman": "SC",
    "South Dakota State": "SD", "South Dakota": "SD",
    "Tennessee": "TN", "Vanderbilt": "TN", "Memphis": "TN", "Middle Tennessee": "TN",
    "Tennessee State": "TN", "Chattanooga": "TN",
    "Texas": "TX", "Texas A&M": "TX", "TCU": "TX", "Baylor": "TX", "Houston": "TX",
    "Texas Tech": "TX", "SMU": "TX", "Rice": "TX", "North Texas": "TX",
    "UTSA": "TX", "UTEP": "TX", "Sam Houston State": "TX", "Texas State": "TX",
    "Utah": "UT", "Utah State": "UT", "BYU": "UT", "Weber State": "UT",
    "Virginia": "VA", "Virginia Tech": "VA", "James Madison": "VA",
    "Old Dominion": "VA", "Liberty": "VA", "Richmond": "VA", "William & Mary": "VA",
    "Washington": "WA", "Washington State": "WA", "Eastern Washington": "WA",
    "West Virginia": "WV", "Marshall": "WV",
    "Wisconsin": "WI", "Wisconsin-Whitewater": "WI",
    "Wyoming": "WY",
}

# Where each NFL team plays, for matching against the above.
TEAM_STATE: dict[str, str] = {
    "ARI": "AZ", "ATL": "GA", "BAL": "MD", "BUF": "NY", "CAR": "NC", "CHI": "IL",
    "CIN": "OH", "CLE": "OH", "DAL": "TX", "DEN": "CO", "DET": "MI", "GB": "WI",
    "HOU": "TX", "IND": "IN", "JAX": "FL", "KC": "MO", "LAC": "CA", "LAR": "CA",
    "LV": "NV", "MIA": "FL", "MIN": "MN", "NE": "MA", "NO": "LA", "NYG": "NJ",
    "NYJ": "NJ", "PHI": "PA", "PIT": "PA", "SEA": "WA", "SF": "CA", "TB": "FL",
    "TEN": "TN", "WAS": "MD",
}


# Naming variants that appear in roster data.
ALIASES = {
    "n.c. state": "NC State",
    "nc state": "NC State",
    "north carolina state": "NC State",
    "ole miss": "Ole Miss",
    "southern california": "USC",
    "miami (fla.)": "Miami (FL)",
    "miami fl": "Miami (FL)",
    "miami oh": "Miami (OH)",
    "texas christian": "TCU",
    "brigham young": "BYU",
    "louisiana state": "LSU",
    "central florida": "UCF",
    "pittsburgh": "Pittsburgh",
    "pitt": "Pittsburgh",
    "st. john's": None,
}


def _lookup(name: str) -> str | None:
    cleaned = name.strip()
    if not cleaned:
        return None

    direct = COLLEGE_STATE.get(cleaned)
    if direct:
        return direct

    # Strip trailing descriptors: "Jackson State University" -> "Jackson State".
    trimmed = cleaned
    for suffix in (" University", " College", " Univ."):
        if trimmed.endswith(suffix):
            trimmed = trimmed[: -len(suffix)].strip()
    if trimmed in COLLEGE_STATE:
        return COLLEGE_STATE[trimmed]

    alias = ALIASES.get(cleaned.lower())
    if alias:
        return COLLEGE_STATE.get(alias)

    return None


def resolve_college(college: str) -> tuple[str, str] | None:
    """Return the (program, state) that actually matched, or None.

    Transfers are recorded as "First; Second". The last entry is where the
    player finished, so it is tried first. Returning the matched program — not
    just the state — matters: naming the last school in the list while having
    matched on an earlier one produces a claim that is quietly wrong.
    """
    raw = (college or "").strip()
    if not raw:
        return None

    parts = [p.strip() for p in raw.split(";") if p.strip()]
    for part in reversed(parts):
        found = _lookup(part)
        if found:
            return part, found
    return None


def state_for_college(college: str) -> str | None:
    """The state a program is in, or None when it is not mapped."""
    resolved = resolve_college(college)
    return resolved[1] if resolved else None


def state_for_team(team: str) -> str | None:
    return TEAM_STATE.get((team or "").strip().upper())


def coverage(colleges: list[str]) -> tuple[int, int]:
    """How many of the given colleges are mapped. Used by QA."""
    mapped = sum(1 for c in colleges if state_for_college(c))
    return mapped, len(colleges)
