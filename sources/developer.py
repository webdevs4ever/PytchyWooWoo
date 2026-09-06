"""Build, verify, and release automation.

The Developer role from ARCHITECTURE.md, implemented as a plain module. It owns
the path from working tree to pushed commit, with the checks that should never
be skipped wired in as gates rather than left to memory.

Design constraint: **pushing is never implicit.** Every function that writes to
a remote defaults to a dry run and requires an explicit opt-in. A module that
can silently publish is a module that eventually publishes something it
shouldn't — a half-finished refactor, a secret, a stale branch.
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    # Allow `python sources/developer.py` as well as `python -m sources.developer`:
    # running a file inside a package leaves the repo root off sys.path.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sources"


import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sources import qa

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files that must never be committed regardless of .gitignore state.
FORBIDDEN_PATHS = re.compile(r"(^|/)(\.env|\.venv/|__pycache__/|\.cache/|salaries/.*\.csv)")

# Patterns that indicate a credential or personal detail in tracked source.
SECRET_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email address"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "API key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"""(?i)\b(api[_-]?key|secret|token|password)\s*=\s*["'][^"'{}$]{8,}["']"""),
     "hardcoded credential"),
]

# Placeholder values that match a pattern but are deliberately not secrets.
SECRET_ALLOWLIST = re.compile(r"noreply@|example\.com|set NWS_USER_AGENT|your[_-]?key")


class ReleaseBlocked(Exception):
    """Raised when a gate fails and the release must not proceed."""


def run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run a command in the repo root, capturing output."""
    return subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True, check=check
    )


# --- Repository state -------------------------------------------------------


def current_branch() -> str:
    return run("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def changed_paths() -> list[str]:
    """Paths with staged, unstaged, or untracked changes."""
    result = run("git", "status", "--porcelain")
    return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]


def is_clean() -> bool:
    return not changed_paths()


# --- Gates ------------------------------------------------------------------


def check_forbidden_paths(paths: list[str] | None = None) -> list[qa.Issue]:
    """Refuse to commit generated or secret-bearing paths."""
    paths = paths if paths is not None else changed_paths()
    return [
        qa.Issue(
            qa.IssueLevel.ERROR,
            "release.forbidden_path",
            f"{path} must never be committed",
            "developer",
        )
        for path in paths
        if FORBIDDEN_PATHS.search(path)
    ]


def scan_for_secrets(paths: list[str] | None = None) -> list[qa.Issue]:
    """Grep tracked Python and Markdown for credentials and personal details.

    This exists because it has already caught something real: a personal email
    hardcoded as a default User-Agent, one command short of being published.
    """
    if paths is None:
        tracked = run("git", "ls-files", "*.py", "*.md", "*.json").stdout.splitlines()
        paths = [p for p in tracked if p]

    issues: list[qa.Issue] = []
    for path in paths:
        full = REPO_ROOT / path
        if not full.exists() or full.is_dir():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for line_number, line in enumerate(text.splitlines(), start=1):
            if SECRET_ALLOWLIST.search(line):
                continue
            for pattern, label in SECRET_PATTERNS:
                if pattern.search(line):
                    issues.append(
                        qa.Issue(
                            qa.IssueLevel.ERROR,
                            "release.secret",
                            f"{path}:{line_number} looks like a {label}",
                            "developer",
                        )
                    )
                    break
    return issues


def check_imports() -> list[qa.Issue]:
    """Every module must import cleanly — a syntax error should not reach main."""
    modules = [p.stem for p in REPO_ROOT.glob("*.py")] + [
        f"sources.{p.stem}"
        for p in (REPO_ROOT / "sources").glob("*.py")
        # Skip __init__ (nothing to check) and this module (already imported).
        if p.stem not in {"__init__", "developer"}
    ]

    issues: list[qa.Issue] = []
    for module in sorted(modules):
        result = run(sys.executable, "-c", f"import {module}")
        if result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
            issues.append(
                qa.Issue(
                    qa.IssueLevel.ERROR,
                    "release.import_failed",
                    f"{module}: {detail}",
                    "developer",
                )
            )
    return issues


def smoke_test() -> list[qa.Issue]:
    """Run the CLI fully offline; it must exit zero without network access."""
    result = run(
        sys.executable, "main.py", "--no-weather", "--no-splits", "--no-pricing", "--quiet"
    )
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        return [
            qa.Issue(
                qa.IssueLevel.ERROR,
                "release.smoke_failed",
                f"offline CLI run exited {result.returncode}: {detail}",
                "developer",
            )
        ]
    return []


def check_rules() -> list[qa.Issue]:
    """Scoring rules must be internally consistent before a release."""
    from sources import manager

    return [i for i in manager.validate_rule_sets() if i.level is qa.IssueLevel.ERROR]


def check_override_precedence() -> list[qa.Issue]:
    """Corrections must always win over upstream. Guarded, not assumed.

    This is a stated invariant rather than an implementation detail, so a
    refactor that quietly reintroduces a fallback should fail the release.
    """
    import overrides as ov
    from models import RosterEntry

    upstream = RosterEntry("Test Player", "AAA", "WR", 1, "ACT")
    override = ov.PlayerOverride(
        key="testplayer", display_name="Test Player", jersey_number=99, team="ZZZ"
    )
    book = ov.Overrides(players={"testplayer": override})

    issues: list[qa.Issue] = []

    contradicts = book.apply("testplayer", upstream)
    if contradicts.jersey_number != 99 or contradicts.team != "ZZZ":
        issues.append(
            qa.Issue(
                qa.IssueLevel.ERROR,
                "release.override_not_authoritative",
                f"correction lost to upstream: got {contradicts}",
                "developer",
            )
        )

    # Unset fields must fall through to upstream, not blank it.
    if contradicts.position != "WR":
        issues.append(
            qa.Issue(
                qa.IssueLevel.ERROR,
                "release.override_clobbers_upstream",
                f"unset field did not fall through: position={contradicts.position!r}",
                "developer",
            )
        )

    # A correction must stand alone when upstream has nothing.
    synthesized = book.apply("testplayer", None)
    if synthesized is None or synthesized.jersey_number != 99:
        issues.append(
            qa.Issue(
                qa.IssueLevel.ERROR,
                "release.override_missing_player",
                "correction did not supply a player absent upstream",
                "developer",
            )
        )

    return issues


GATES = [
    check_forbidden_paths,
    scan_for_secrets,
    check_imports,
    smoke_test,
    check_rules,
    check_override_precedence,
]


@dataclass
class VerifyResult:
    issues: list[qa.Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[qa.Issue]:
        return [i for i in self.issues if i.level is qa.IssueLevel.ERROR]

    @property
    def ok(self) -> bool:
        return not self.errors

    def format(self) -> str:
        if not self.issues:
            return "Verify: all gates passed."
        lines = [str(i) for i in self.issues]
        lines.append("")
        lines.append(f"Verify: {len(self.errors)} blocking issue(s).")
        return "\n".join(lines)


def verify() -> VerifyResult:
    """Run every release gate."""
    issues: list[qa.Issue] = []
    for gate in GATES:
        issues.extend(gate())
    return VerifyResult(issues)


# --- Release ----------------------------------------------------------------


def commit(message: str, dry_run: bool = True) -> str:
    """Stage everything and commit. Verification is the caller's job."""
    if is_clean():
        return "nothing to commit"
    if dry_run:
        return f"[dry run] would commit {len(changed_paths())} path(s)"

    run("git", "add", "-A", check=True)
    body = f"{message}\n\nCo-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
    result = subprocess.run(
        ["git", "commit", "-F", "-"],
        cwd=REPO_ROOT,
        input=body,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseBlocked(f"commit failed: {result.stderr.strip()}")
    return run("git", "log", "--oneline", "-1").stdout.strip()


def push(remote: str = "origin", branch: str | None = None, dry_run: bool = True) -> str:
    """Push to a remote. Dry run by default — publishing is always explicit."""
    branch = branch or current_branch()
    if dry_run:
        return f"[dry run] would push {branch} -> {remote}"

    result = run("git", "push", remote, branch)
    if result.returncode != 0:
        raise ReleaseBlocked(f"push failed: {result.stderr.strip()}")
    return (result.stderr or result.stdout).strip().splitlines()[-1]


def release(message: str, dry_run: bool = True, remote: str = "origin") -> list[str]:
    """Verify, commit, push — aborting at the first failed gate."""
    result = verify()
    if not result.ok:
        raise ReleaseBlocked(result.format())

    steps = ["verify: passed"]
    steps.append(f"commit: {commit(message, dry_run=dry_run)}")
    steps.append(f"push:   {push(remote=remote, dry_run=dry_run)}")
    return steps


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Verify and release.")
    parser.add_argument("--verify", action="store_true", help="run gates only")
    parser.add_argument("-m", "--message", help="commit message for a release")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually commit and push (default is a dry run)",
    )
    args = parser.parse_args(argv)

    if args.verify or not args.message:
        result = verify()
        print(result.format())
        return 0 if result.ok else 1

    try:
        for step in release(args.message, dry_run=not args.execute):
            print(step)
    except ReleaseBlocked as exc:
        print(f"Release blocked.\n{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
