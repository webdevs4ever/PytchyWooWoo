"""Admin console — the only component permitted to write manual corrections.

Read access to overrides is open: any module may call `overrides.load()`. Write
access is not. Every mutation goes through this console, so there is exactly one
code path that can change a correction and exactly one place that records who
changed what and why.

Two guards enforce that:

1. **Root only.** The console refuses to run outside the repository root. A
   correction applied from a subdirectory would write a second `overrides.json`
   that the rest of the app never reads — silently doing nothing.
2. **Explicit writes.** Every mutation prints the before/after and asks for
   confirmation. `--yes` skips the prompt for scripted use; nothing skips the
   audit entry.

Usage:
    python admin.py                       interactive console
    python admin.py list
    python admin.py inspect "Amon-Ra St. Brown"
    python admin.py set "Player Name" --number 14 --team DET --note "why"
    python admin.py remove "Player Name"
    python admin.py audit
    python admin.py check
"""

from __future__ import annotations

import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import overrides as ov
from overrides import OVERRIDES_PATH, PlayerOverride

REPO_ROOT = Path(__file__).resolve().parent


class NotRoot(Exception):
    """Raised when the console is invoked outside the repository root."""


def require_root() -> None:
    """Refuse to operate anywhere but the repository root."""
    cwd = Path.cwd().resolve()
    if cwd != REPO_ROOT:
        raise NotRoot(
            f"admin console must be run from the repository root.\n"
            f"  root: {REPO_ROOT}\n"
            f"  cwd:  {cwd}\n"
            f"Run: cd {REPO_ROOT} && python admin.py"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _strict(name: str) -> str:
    from sources.odds import strict_name

    return strict_name(name)


def _write(overrides: ov.Overrides) -> None:
    """Persist corrections. The single write path in the codebase."""
    require_root()
    overrides.updated_at = _now()
    payload = ov.serialize(overrides)
    OVERRIDES_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _record(overrides: ov.Overrides, action: str, key: str, detail: dict) -> None:
    overrides.history.append(
        {"at": _now(), "by": _actor(), "action": action, "key": key, "detail": detail}
    )


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# --- Commands ---------------------------------------------------------------


def cmd_list() -> int:
    overrides = ov.load()
    if not overrides.players:
        print("No overrides. Upstream data is used as-is.")
        return 0

    print(f"{len(overrides.players)} override(s) in {OVERRIDES_PATH.name}:\n")
    for key, o in sorted(overrides.players.items()):
        fields = ", ".join(f"{k}={v!r}" for k, v in sorted(o.fields().items()))
        print(f"  {o.display_name or key}")
        print(f"    {fields}")
        if o.note:
            print(f"    note: {o.note}")
        if o.added_at:
            print(f"    set {o.added_at} by {o.added_by or 'unknown'}")
        print()
    return 0


def cmd_inspect(name: str) -> int:
    """Show upstream, override, and effective values side by side."""
    key = _strict(name)
    overrides = ov.load()
    override = overrides.get(key)

    print(f"{name}   (key: {key})\n")

    try:
        from sources.rosters import get_book

        upstream = get_book().entry_upstream(key)
    except Exception as exc:
        print(f"  upstream:  unavailable ({exc})")
        upstream = None
    else:
        print(f"  upstream:  {upstream if upstream else 'not found'}")

    if override is None:
        print("  override:  none")
        print("\n  Effective values come straight from upstream.")
        return 0

    print(f"  override:  {override.fields()}")
    if override.note:
        print(f"  note:      {override.note}")

    effective = overrides.apply(key, upstream)
    print(f"  effective: {effective}")

    redundant = [
        f
        for f, v in override.fields().items()
        if f != "display_name" and upstream and getattr(upstream, f, None) == v
    ]
    if redundant:
        print(
            f"\n  Upstream now agrees on {', '.join(redundant)} — "
            "this override is obsolete."
        )
    return 0


def cmd_set(name: str, args, assume_yes: bool = False) -> int:
    require_root()

    key = _strict(name)
    if not key:
        print("A player name is required.", file=sys.stderr)
        return 1

    changes = {
        k: v
        for k, v in {
            "display_name": name,
            "jersey_number": args.number,
            "team": args.team.upper() if args.team else None,
            "position": args.position.upper() if args.position else None,
            "status": args.status.upper() if args.status else None,
        }.items()
        if v is not None
    }
    if set(changes) == {"display_name"}:
        print("Nothing to set. Pass at least one of --number/--team/--position/--status.",
              file=sys.stderr)
        return 1

    overrides = ov.load()
    existing = overrides.get(key)

    print(f"\n{name}   (key: {key})")
    try:
        from sources.rosters import get_book

        upstream = get_book().entry_upstream(key)
        print(f"  upstream: {upstream if upstream else 'not found'}")
    except Exception as exc:
        upstream = None
        print(f"  upstream: unavailable ({exc})")

    print(f"  before:   {existing.fields() if existing else 'no override'}")
    print(f"  after:    {changes}")

    if not _confirm("\nApply this override?", assume_yes):
        print("Cancelled. Nothing written.")
        return 1

    overrides.players[key] = PlayerOverride(
        key=key,
        display_name=name,
        jersey_number=changes.get("jersey_number"),
        team=changes.get("team"),
        position=changes.get("position"),
        status=changes.get("status"),
        note=args.note or (existing.note if existing else ""),
        added_at=_now(),
        added_by=_actor(),
    )
    _record(overrides, "set", key, changes)
    _write(overrides)
    print(f"Written to {OVERRIDES_PATH.name}.")
    return 0


def cmd_remove(name: str, assume_yes: bool = False) -> int:
    require_root()

    key = _strict(name)
    overrides = ov.load()
    existing = overrides.get(key)
    if existing is None:
        print(f"No override for {name!r}.")
        return 1

    print(f"\n{name}   (key: {key})")
    print(f"  removing: {existing.fields()}")
    if not _confirm("\nRemove this override?", assume_yes):
        print("Cancelled. Nothing written.")
        return 1

    del overrides.players[key]
    _record(overrides, "remove", key, existing.fields())
    _write(overrides)
    print(f"Removed. {len(overrides.players)} override(s) remain.")
    return 0


def cmd_audit(limit: int = 20) -> int:
    overrides = ov.load()
    if not overrides.history:
        print("No history recorded.")
        return 0

    print(f"Last {min(limit, len(overrides.history))} change(s):\n")
    for entry in overrides.history[-limit:]:
        detail = ", ".join(f"{k}={v!r}" for k, v in sorted((entry.get("detail") or {}).items()))
        print(f"  {entry.get('at','?')}  {entry.get('by','?'):12} "
              f"{entry.get('action','?'):7} {entry.get('key','?')}")
        if detail:
            print(f"    {detail}")
    return 0


def cmd_status() -> int:
    """Run-level health, moved here from the dashboard.

    These counts are operator information — how much of the slate resolved and
    how much of it is degraded. A person reading the board wants to know which
    players to pick, not how many notices the last run produced.
    """
    import main as pipeline
    import manager

    slates = pipeline.build_demo_slates()
    pipeline.enrich_with_rosters(slates, quiet=True)
    pipeline.enrich_with_weather(slates, quiet=True)
    pipeline.enrich_with_splits(slates, quiet=True)
    pipeline.enrich_with_pricing(slates, quiet=True)

    view = manager.build_view(slates)
    q = view["quality"]

    print("Slate status\n")
    for label, value in [
        ("Players", q["players"]),
        ("Flags", q["flags"]),
        ("Errors", q["errors"]),
        ("Warnings", q["warnings"]),
        ("Notices", q["notices"]),
    ]:
        print(f"  {label:10} {value:>4}")

    print(f"\n  Splits season   {view['splits_season']}")
    for name, values in sorted(view["scoring"].items()):
        print(f"  {name:15} ${values['salary_cap']:,} cap, "
              f"{values['points_per_reception']} PPR")

    print("\nBy position\n")
    for card in view["cards"]:
        priced = sum(1 for r in card["players"] if r["pricing"])
        flags = sum(len(r["flags"]) for r in card["players"])
        print(f"  {card['label']:16} {len(card['players']):>2} player(s), "
              f"{priced} priced, {flags} flag(s)")

    print(f"\n{'OK' if q['ok'] else 'ERRORS PRESENT — see qa.py'}")
    return 0 if q["ok"] else 1


def cmd_check() -> int:
    """Validate corrections against upstream."""
    issues = ov.validate()
    if not issues:
        print("Overrides: no issues.")
        return 0
    for issue in issues:
        print(issue)
    print(f"\n{len(issues)} issue(s).")
    return 0


# --- Interactive ------------------------------------------------------------

MENU = """
Admin console — manual data corrections
  1  list overrides
  2  inspect a player
  3  set an override
  4  remove an override
  5  audit log
  6  check overrides against upstream
  7  slate status
  q  quit
"""


def interactive() -> int:
    require_root()
    print(MENU)

    class Args:
        number = team = position = status = note = None

    while True:
        try:
            choice = input("admin> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice in {"q", "quit", "exit"}:
            return 0
        if choice == "1":
            cmd_list()
        elif choice == "2":
            cmd_inspect(input("  player name: ").strip())
        elif choice == "3":
            name = input("  player name: ").strip()
            args = Args()
            raw_number = input("  jersey number (blank to skip): ").strip()
            args.number = int(raw_number) if raw_number.isdigit() else None
            args.team = input("  team (blank to skip): ").strip() or None
            args.position = input("  position (blank to skip): ").strip() or None
            args.status = input("  status (blank to skip): ").strip() or None
            args.note = input("  note (why): ").strip() or None
            cmd_set(name, args)
        elif choice == "4":
            cmd_remove(input("  player name: ").strip())
        elif choice == "5":
            cmd_audit()
        elif choice == "6":
            cmd_check()
        elif choice == "7":
            cmd_status()
        else:
            print(MENU)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Admin console for manual corrections.")
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="list all overrides")

    p_inspect = sub.add_parser("inspect", help="compare upstream, override, effective")
    p_inspect.add_argument("name")

    p_set = sub.add_parser("set", help="create or replace an override")
    p_set.add_argument("name")
    p_set.add_argument("--number", type=int, help="jersey number")
    p_set.add_argument("--team")
    p_set.add_argument("--position")
    p_set.add_argument("--status")
    p_set.add_argument("--note", help="why this correction exists")
    p_set.add_argument("-y", "--yes", action="store_true", help="skip confirmation")

    p_remove = sub.add_parser("remove", help="delete an override")
    p_remove.add_argument("name")
    p_remove.add_argument("-y", "--yes", action="store_true", help="skip confirmation")

    p_audit = sub.add_parser("audit", help="show the change log")
    p_audit.add_argument("--limit", type=int, default=20)

    sub.add_parser("check", help="validate overrides against upstream")
    sub.add_parser("status", help="slate health: players, flags, and QA counts")

    args = parser.parse_args(argv)

    try:
        if args.command is None:
            return interactive()
        if args.command == "list":
            return cmd_list()
        if args.command == "inspect":
            return cmd_inspect(args.name)
        if args.command == "set":
            return cmd_set(args.name, args, assume_yes=args.yes)
        if args.command == "remove":
            return cmd_remove(args.name, assume_yes=args.yes)
        if args.command == "audit":
            return cmd_audit(args.limit)
        if args.command == "check":
            return cmd_check()
        if args.command == "status":
            return cmd_status()
    except NotRoot as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
