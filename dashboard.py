"""HTML renderer for the dashboard view model.

Takes `manager.build_view()` and emits a self-contained page. No framework, no
external assets — the output is one file that opens anywhere.

Presentation decisions live here; the view model stays a plain data contract, so
a different front end can consume the same payload without touching this file.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import config

SEVERITY_COLORS = {
    "critical": "var(--sev-critical)",
    "warning": "var(--sev-warning)",
    "info": "var(--sev-info)",
}
LEVEL_COLORS = {
    "error": "var(--sev-critical)",
    "warning": "var(--sev-warning)",
    "notice": "var(--sev-info)",
}
PLATFORM_LABELS = {"draftkings": "DK", "fanduel": "FD"}

CSS = """
*, *::before, *::after { box-sizing: border-box; }

/* Dark-first: this page commits to the broadcast-board look, so :root is the
   dark palette and the light variant is the override. */
:root {
  --ground: #0a0a0c;
  --surface: #17171c;
  --surface-2: #1f1f27;
  --edge: #2c2c36;
  --ink: #f4f2ef;
  --muted: #8d8a95;
  --dim: #66636e;
  --brand: #f0522c;
  --gold: #c8a15a;
  --sev-critical: #ff5a36;
  --sev-warning: #e8a33d;
  --sev-info: #5a8ed6;
  --avatar-from: #d94f2b;
  --avatar-to: #3f4c8c;
  --shadow: 0 1px 0 rgba(255,255,255,0.03) inset, 0 8px 24px rgba(0,0,0,0.45);
}

@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --ground: #edecea;
    --surface: #ffffff;
    --surface-2: #f4f3f1;
    --edge: #ddd9d4;
    --ink: #1a181c;
    --muted: #6b6772;
    --dim: #928d99;
    --brand: #c93d18;
    --gold: #9a7738;
    --sev-critical: #cc3d1c;
    --sev-warning: #9a6c12;
    --sev-info: #2f5fa8;
    --avatar-from: #c9451f;
    --avatar-to: #35407a;
    --shadow: 0 1px 2px rgba(0,0,0,0.06), 0 8px 20px rgba(0,0,0,0.06);
  }
}

:root[data-theme="light"] {
  --ground: #edecea;
  --surface: #ffffff;
  --surface-2: #f4f3f1;
  --edge: #ddd9d4;
  --ink: #1a181c;
  --muted: #6b6772;
  --dim: #928d99;
  --brand: #c93d18;
  --gold: #9a7738;
  --sev-critical: #cc3d1c;
  --sev-warning: #9a6c12;
  --sev-info: #2f5fa8;
  --avatar-from: #c9451f;
  --avatar-to: #35407a;
  --shadow: 0 1px 2px rgba(0,0,0,0.06), 0 8px 20px rgba(0,0,0,0.06);
}

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 15px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

.shell { max-width: 1240px; margin: 0 auto; padding: 40px 24px 72px; }

/* --- Masthead --- */
.masthead {
  display: flex; flex-wrap: wrap; align-items: flex-end;
  justify-content: space-between; gap: 20px;
  padding-bottom: 20px; border-bottom: 2px solid var(--brand);
}
.wordmark {
  margin: 0;
  font-size: clamp(28px, 5vw, 42px);
  font-weight: 800;
  letter-spacing: -0.02em;
  text-transform: uppercase;
  line-height: 0.95;
  text-wrap: balance;
}
.wordmark span { color: var(--brand); }
.masthead-meta {
  display: flex; flex-wrap: wrap; gap: 8px 20px;
  font-size: 12px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.08em;
}
.masthead-meta b { color: var(--ink); font-weight: 600; }

/* --- Quality summary --- */
.summary { display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0 32px; }
.chip {
  display: inline-flex; align-items: baseline; gap: 8px;
  padding: 7px 13px;
  background: var(--surface); border: 1px solid var(--edge);
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--muted);
}
.chip b {
  font-size: 15px; font-weight: 700; color: var(--ink);
  font-variant-numeric: tabular-nums;
}
.chip[data-tone="error"] b { color: var(--sev-critical); }
.chip[data-tone="warning"] b { color: var(--sev-warning); }
.chip[data-tone="notice"] b { color: var(--sev-info); }

/* --- Grid --- */
.grid {
  display: grid; gap: 18px;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
}

/* --- Card --- */
.card {
  position: relative;
  display: flex; flex-direction: column; gap: 14px;
  padding: 22px 20px 18px;
  background: var(--surface);
  border: 1px solid var(--edge);
  border-top: 3px solid var(--card-accent, var(--edge));
  box-shadow: var(--shadow);
}
.card[data-playable="false"] { opacity: 0.62; }

.identity { display: flex; align-items: center; gap: 15px; }
.avatar {
  flex: 0 0 auto;
  width: 62px; height: 62px; border-radius: 50%;
  display: grid; place-items: center;
  background: linear-gradient(145deg, var(--avatar-from), var(--avatar-to));
  border: 2px solid var(--gold);
  font-size: 21px; font-weight: 800; letter-spacing: 0.02em;
  color: #fff;
}
.who { min-width: 0; }
.name {
  margin: 0;
  font-size: 17px; font-weight: 800;
  text-transform: uppercase; letter-spacing: 0.005em;
  line-height: 1.15;
}
.slot {
  margin-top: 3px;
  font-size: 11px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.1em;
}
.jersey {
  margin-left: auto; align-self: flex-start;
  font-size: 30px; font-weight: 800; color: var(--brand);
  font-variant-numeric: tabular-nums; letter-spacing: -0.03em;
}

.conditions {
  padding: 9px 12px;
  background: var(--surface-2);
  font-size: 12px; color: var(--muted);
  display: flex; justify-content: space-between; gap: 12px;
}
.conditions b { color: var(--ink); font-weight: 600; }

.flags { display: flex; flex-direction: column; gap: 7px; }
.flag {
  display: grid; grid-template-columns: 3px 1fr; gap: 10px;
  font-size: 12.5px; line-height: 1.4; color: var(--ink);
}
.flag::before { content: ""; background: var(--flag-color); }
.flag-code {
  display: block; font-size: 10px; color: var(--flag-color);
  text-transform: uppercase; letter-spacing: 0.09em; font-weight: 700;
  margin-bottom: 1px;
}
.quiet { font-size: 12.5px; color: var(--dim); font-style: italic; }

.pricing { border-top: 1px solid var(--edge); padding-top: 12px; }
.price-row {
  display: grid; grid-template-columns: 34px 1fr auto auto;
  gap: 12px; align-items: baseline;
  padding: 4px 0;
  font-size: 13px; font-variant-numeric: tabular-nums;
}
.plat {
  font-size: 10px; font-weight: 800; color: var(--muted);
  letter-spacing: 0.08em;
}
.salary { color: var(--ink); }
.proj { color: var(--muted); font-size: 12px; }
.value { font-weight: 700; color: var(--ink); }
.price-row[data-best="true"] .value { color: var(--brand); }
.price-row[data-best="true"] .plat { color: var(--brand); }

.issues { border-top: 1px solid var(--edge); padding-top: 11px; display: flex; flex-direction: column; gap: 6px; }
.issue { font-size: 11.5px; line-height: 1.4; color: var(--muted); }
.issue b {
  color: var(--issue-color); font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.07em; font-size: 10px;
}

.badge {
  position: absolute; top: 14px; right: 16px;
  padding: 3px 8px;
  background: var(--sev-critical); color: #fff;
  font-size: 9.5px; font-weight: 800;
  text-transform: uppercase; letter-spacing: 0.11em;
}

.colophon {
  margin-top: 40px; padding-top: 18px; border-top: 1px solid var(--edge);
  font-size: 12px; color: var(--dim); line-height: 1.6;
}
.colophon code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11.5px; color: var(--muted);
}
"""


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _render_flags(flags: list[dict]) -> str:
    if not flags:
        return '<p class="quiet">No flags — nothing cleared a threshold.</p>'
    rows = []
    for flag in flags:
        color = SEVERITY_COLORS.get(flag["severity"], "var(--sev-info)")
        rows.append(
            f'<div class="flag" style="--flag-color:{color}">'
            f'<span><span class="flag-code">{_esc(flag["code"])}</span>'
            f'{_esc(flag["reason"])}</span></div>'
        )
    return f'<div class="flags">{"".join(rows)}</div>'


def _render_pricing(pricing: list[dict]) -> str:
    if not pricing:
        return (
            '<div class="pricing"><p class="quiet">No salary loaded — '
            "drop a contest export in salaries/.</p></div>"
        )

    best = max(pricing, key=lambda p: p["value"])["platform"] if len(pricing) > 1 else None
    rows = []
    for entry in pricing:
        rows.append(
            f'<div class="price-row" data-best="{str(entry["platform"] == best).lower()}">'
            f'<span class="plat">{PLATFORM_LABELS.get(entry["platform"], entry["platform"][:2].upper())}</span>'
            f'<span class="salary">${entry["salary"]:,}</span>'
            f'<span class="proj">{entry["projected"]:.1f} proj</span>'
            f'<span class="value">{entry["value"]:.2f}<span class="proj"> /$1k</span></span>'
            f"</div>"
        )
    return f'<div class="pricing">{"".join(rows)}</div>'


def _render_issues(issues: list[dict]) -> str:
    if not issues:
        return ""
    rows = []
    for issue in issues:
        color = LEVEL_COLORS.get(issue["level"], "var(--sev-info)")
        rows.append(
            f'<p class="issue" style="--issue-color:{color}">'
            f'<b>{_esc(issue["level"])}</b> {_esc(issue["message"])}</p>'
        )
    return f'<div class="issues">{"".join(rows)}</div>'


def _render_card(card: dict) -> str:
    badge = '<span class="badge">Ruled out</span>' if not card["playable"] else ""
    conditions = (
        f'<div class="conditions"><span>{_esc(card["matchup"])}</span>'
        f'<b>{_esc(card["conditions"])}</b></div>'
        if card["conditions"]
        else f'<div class="conditions"><span>{_esc(card["matchup"])}</span>'
        f'<b>No forecast</b></div>'
    )

    return (
        f'<article class="card" style="--card-accent:{_esc(card["accent"])}" '
        f'data-playable="{str(card["playable"]).lower()}">'
        f"{badge}"
        f'<div class="identity">'
        f'<div class="avatar">{_esc(card["initials"])}</div>'
        f'<div class="who"><h2 class="name">{_esc(card["name"])}</h2>'
        f'<div class="slot">{_esc(card["position"])} · {_esc(card["team"])}</div></div>'
        f'<div class="jersey">{_esc(card["number"])}</div>'
        f"</div>"
        f"{conditions}"
        f'{_render_flags(card["flags"])}'
        f'{_render_pricing(card["pricing"])}'
        f'{_render_issues(card["issues"])}'
        f"</article>"
    )


def render(view: dict) -> str:
    """Return the full page for a view model from `manager.build_view()`."""
    quality = view["quality"]
    scoring = view["scoring"]

    chips = [
        ("Players", len(view["cards"]), None),
        ("Flags", sum(len(c["flags"]) for c in view["cards"]), None),
        ("Errors", quality["errors"], "error"),
        ("Warnings", quality["warnings"], "warning"),
        ("Notices", quality["notices"], "notice"),
    ]
    chip_html = "".join(
        f'<span class="chip"{f" data-tone={tone}" if tone else ""}>'
        f"{label}<b>{count}</b></span>"
        for label, count, tone in chips
    )

    caps = " · ".join(
        f"{PLATFORM_LABELS.get(name, name)} ${values['salary_cap']:,} cap, "
        f"{values['points_per_reception']} PPR"
        for name, values in sorted(scoring.items())
    )

    return f"""<title>Fantasy Bet Helper</title>
<style>{CSS}</style>
<div class="shell">
  <header class="masthead">
    <h1 class="wordmark">Fantasy<span>&nbsp;Bet</span><br>Helper</h1>
    <div class="masthead-meta">
      <span>Splits <b>{view["splits_season"]}</b></span>
      <span>Generated <b>{_esc(view["generated_at"][:16].replace("T", " "))} UTC</b></span>
      <span>{_esc(caps)}</span>
    </div>
  </header>

  <div class="summary">{chip_html}</div>

  <main class="grid">{"".join(_render_card(c) for c in view["cards"])}</main>

  <footer class="colophon">
    Weather from the National Weather Service. Splits, rosters, and injury
    designations from nflverse. Salaries from DraftKings and FanDuel contest
    exports; projections fall back to each export's season average when
    <code>ODDS_API_KEY</code> is unset. Manual corrections in
    <code>overrides.json</code> always take precedence over upstream data.
  </footer>
</div>
"""


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Render the dashboard to HTML.")
    parser.add_argument("-o", "--output", default="dashboard.html")
    args = parser.parse_args(argv)

    import main as pipeline
    import manager

    slates = pipeline.build_demo_slates()
    pipeline.enrich_with_rosters(slates, quiet=True)
    pipeline.enrich_with_weather(slates, quiet=True)
    pipeline.enrich_with_splits(slates, quiet=True)
    pipeline.enrich_with_pricing(slates, quiet=True)

    Path(args.output).write_text(render(manager.build_view(slates)), encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
