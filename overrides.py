"""Manual corrections layered over upstream data — read side.

Upstream feeds carry errors: a wrong jersey number, a stale team after a trade,
a player missing from a roster file. This module lets those be corrected locally
without editing source, and records why each correction exists.

**This module is read-only by design.** Nothing here writes `overrides.json`.
Write authority lives solely in `admin.py`, so there is exactly one path that
can mutate corrections and exactly one place that logs who changed what. Any
module may read overrides; none may set them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import config
from models import RosterEntry

OVERRIDES_PATH = Path(__file__).resolve().parent / "overrides.json"
SCHEMA_VERSION = 1

# Fields a correction may set. Anything else is rejected as a typo rather than
# silently ignored.
EDITABLE_FIELDS = {"display_name", "jersey_number", "team", "position", "status"}


class OverrideError(Exception):
    """Raised when the overrides file is malformed."""


@dataclass
class PlayerOverride:
    key: str
    display_name: str = ""
    jersey_number: int | None = None
    team: str | None = None
    position: str | None = None
    status: str | None = None
    note: str = ""
    added_at: str = ""
    added_by: str = ""

    def fields(self) -> dict:
        """Only the corrections actually set, ignoring metadata."""
        return {
            name: getattr(self, name)
            for name in EDITABLE_FIELDS
            if getattr(self, name) not in (None, "")
        }

    def apply_to(self, entry: RosterEntry) -> RosterEntry:
        """Return `entry` with this correction layered on top."""
        return RosterEntry(
            player_name=self.display_name or entry.player_name,
            team=self.team or entry.team,
            position=self.position or entry.position,
            jersey_number=(
                self.jersey_number if self.jersey_number is not None else entry.jersey_number
            ),
            status=self.status or entry.status,
        )

    def as_entry(self) -> RosterEntry:
        """Build an entry from scratch, for a player absent upstream."""
        return RosterEntry(
            player_name=self.display_name or self.key,
            team=self.team or "",
            position=self.position or "",
            jersey_number=self.jersey_number,
            status=self.status or "",
        )


@dataclass
class Overrides:
    players: dict[str, PlayerOverride] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    updated_at: str = ""

    def get(self, strict_key: str) -> PlayerOverride | None:
        return self.players.get(strict_key)

    def apply(self, strict_key: str, entry: RosterEntry | None) -> RosterEntry | None:
        """Layer any correction for `strict_key` over `entry`.

        Returns a synthesized entry when the player is absent upstream but a
        correction supplies enough to build one.
        """
        override = self.players.get(strict_key)
        if override is None:
            return entry
        if entry is None:
            return override.as_entry()
        return override.apply_to(entry)

    def __len__(self) -> int:
        return len(self.players)


def _parse(payload: dict) -> Overrides:
    version = payload.get("version")
    if version != SCHEMA_VERSION:
        raise OverrideError(
            f"overrides.json is schema version {version!r}, expected {SCHEMA_VERSION}"
        )

    players: dict[str, PlayerOverride] = {}
    for key, raw in (payload.get("players") or {}).items():
        unknown = set(raw) - EDITABLE_FIELDS - {"note", "added_at", "added_by"}
        if unknown:
            raise OverrideError(
                f"override {key!r} has unknown field(s): {', '.join(sorted(unknown))}"
            )
        number = raw.get("jersey_number")
        if number is not None and not isinstance(number, int):
            raise OverrideError(f"override {key!r}: jersey_number must be an integer")

        players[key] = PlayerOverride(
            key=key,
            display_name=raw.get("display_name", ""),
            jersey_number=number,
            team=raw.get("team"),
            position=raw.get("position"),
            status=raw.get("status"),
            note=raw.get("note", ""),
            added_at=raw.get("added_at", ""),
            added_by=raw.get("added_by", ""),
        )

    return Overrides(
        players=players,
        history=payload.get("history") or [],
        updated_at=payload.get("updated_at", ""),
    )


def load(path: Path | None = None) -> Overrides:
    """Read corrections from disk. A missing file means no corrections."""
    path = path or OVERRIDES_PATH
    if not path.exists():
        return Overrides()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OverrideError(f"overrides.json is not valid JSON: {exc}") from exc

    return _parse(payload)


def serialize(overrides: Overrides) -> dict:
    """The on-disk shape. Used by admin.py when writing; read here for symmetry."""
    return {
        "version": SCHEMA_VERSION,
        "updated_at": overrides.updated_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "players": {
            key: {
                **({"display_name": o.display_name} if o.display_name else {}),
                **({"jersey_number": o.jersey_number} if o.jersey_number is not None else {}),
                **({"team": o.team} if o.team else {}),
                **({"position": o.position} if o.position else {}),
                **({"status": o.status} if o.status else {}),
                **({"note": o.note} if o.note else {}),
                **({"added_at": o.added_at} if o.added_at else {}),
                **({"added_by": o.added_by} if o.added_by else {}),
            }
            for key, o in sorted(overrides.players.items())
        },
        "history": overrides.history[-200:],
    }


def validate(overrides: Overrides | None = None) -> list:
    """Compare corrections against upstream and report ones worth revisiting.

    The important case is a redundant override: upstream has since been fixed,
    so the correction now restates what the feed already says. It is invisible
    until the feed changes again, at which point it silently overrides a correct
    value with a stale one.
    """
    import qa

    overrides = overrides if overrides is not None else load()
    if not overrides.players:
        return []

    issues: list[qa.Issue] = []

    try:
        from sources.rosters import RostersUnavailable, get_book

        book = get_book()
    except Exception as exc:  # network or import failure
        return [
            qa.Issue(
                qa.IssueLevel.NOTICE,
                "override.unverified",
                f"{len(overrides.players)} override(s) could not be checked "
                f"against upstream: {exc}",
                "overrides",
            )
        ]

    for key, override in sorted(overrides.players.items()):
        subject = override.display_name or key
        upstream = book.entry_upstream(key)

        if upstream is None:
            issues.append(
                qa.Issue(
                    qa.IssueLevel.NOTICE,
                    "override.supplements_missing",
                    "player is absent upstream — this override is the only source",
                    subject,
                )
            )
            continue

        redundant = []
        for name, value in override.fields().items():
            if name == "display_name":
                continue
            upstream_value = getattr(
                upstream, "jersey_number" if name == "jersey_number" else name, None
            )
            if upstream_value == value:
                redundant.append(name)

        if redundant:
            issues.append(
                qa.Issue(
                    qa.IssueLevel.WARNING,
                    "override.redundant",
                    f"upstream now agrees on {', '.join(redundant)} — "
                    "this correction is obsolete and should be removed",
                    subject,
                )
            )

    return issues
