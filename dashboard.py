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
  --toggle-bg: #14141a;
}

@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --ground: #edecea; --surface: #ffffff; --surface-2: #f4f3f1; --edge: #ddd9d4;
    --ink: #1a181c; --muted: #6b6772; --dim: #928d99;
    --brand: #c93d18; --gold: #9a7738;
    --sev-critical: #cc3d1c; --sev-warning: #9a6c12; --sev-info: #2f5fa8;
    --avatar-from: #c9451f; --avatar-to: #35407a;
    --shadow: 0 1px 2px rgba(0,0,0,0.06), 0 8px 20px rgba(0,0,0,0.06);
    --toggle-bg: #e2e0dd;
  }
}

:root[data-theme="light"] {
  --ground: #edecea; --surface: #ffffff; --surface-2: #f4f3f1; --edge: #ddd9d4;
  --ink: #1a181c; --muted: #6b6772; --dim: #928d99;
  --brand: #c93d18; --gold: #9a7738;
  --sev-critical: #cc3d1c; --sev-warning: #9a6c12; --sev-info: #2f5fa8;
  --avatar-from: #c9451f; --avatar-to: #35407a;
  --shadow: 0 1px 2px rgba(0,0,0,0.06), 0 8px 20px rgba(0,0,0,0.06);
  --toggle-bg: #e2e0dd;
}

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 15px; line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

.shell { max-width: 1320px; margin: 0 auto; padding: 40px 24px 72px; }

/* --- Masthead --- */
.masthead {
  display: flex; flex-wrap: wrap; align-items: flex-end;
  justify-content: space-between; gap: 24px;
  padding-bottom: 20px; border-bottom: 2px solid var(--brand);
}
.wordmark {
  margin: 0;
  font-size: clamp(26px, 4.4vw, 38px); font-weight: 800;
  letter-spacing: -0.02em; text-transform: uppercase; line-height: 0.95;
  text-wrap: balance;
}
.wordmark span { color: var(--brand); }
.masthead-right { display: flex; flex-direction: column; align-items: flex-end; gap: 12px; }
.masthead-meta {
  display: flex; flex-wrap: wrap; gap: 6px 18px; justify-content: flex-end;
  font-size: 11.5px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.08em;
}
.masthead-meta b { color: var(--ink); font-weight: 600; }

/* --- Platform toggle --- */
.toggle {
  display: inline-flex; padding: 3px; gap: 3px;
  background: var(--toggle-bg); border: 1px solid var(--edge);
}
.toggle button {
  appearance: none; cursor: pointer;
  padding: 8px 18px; border: 0; background: transparent;
  color: var(--muted);
  font: inherit; font-size: 12px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.09em;
}
.toggle button:hover { color: var(--ink); }
.toggle button[aria-pressed="true"] { background: var(--brand); color: #fff; }
.toggle button:focus-visible { outline: 2px solid var(--sev-info); outline-offset: 2px; }
.toggle-cap {
  font-size: 11px; color: var(--dim);
  text-transform: uppercase; letter-spacing: 0.08em;
  font-variant-numeric: tabular-nums;
}

/* --- Grid of position groups --- */
.grid {
  display: grid; gap: 18px; margin-top: 28px;
  grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
}

.card {
  display: flex; flex-direction: column;
  background: var(--surface);
  border: 1px solid var(--edge);
  border-top: 3px solid var(--card-accent, var(--edge));
  box-shadow: var(--shadow);
}
.card-head {
  display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
  padding: 16px 18px 12px;
  border-bottom: 1px solid var(--edge);
}
.card-title {
  margin: 0;
  font-size: 14px; font-weight: 800;
  text-transform: uppercase; letter-spacing: 0.1em;
}
.card-count {
  font-size: 12px; color: var(--muted);
  font-variant-numeric: tabular-nums;
}

/* --- Player rows within a group --- */
.player {
  display: flex; flex-direction: column; gap: 10px;
  padding: 15px 18px;
}
.player + .player { border-top: 1px solid var(--edge); }
.player[data-playable="false"] { opacity: 0.55; }

.identity { display: flex; align-items: center; gap: 12px; }
.avatar {
  flex: 0 0 auto; width: 42px; height: 42px; border-radius: 50%;
  display: grid; place-items: center;
  background: linear-gradient(145deg, var(--avatar-from), var(--avatar-to));
  border: 2px solid var(--gold);
  font-size: 14px; font-weight: 800; color: #fff;
}
.who { min-width: 0; flex: 1; }
.name {
  margin: 0; font-size: 14.5px; font-weight: 800;
  text-transform: uppercase; line-height: 1.15;
}
.slot {
  margin-top: 2px; font-size: 10.5px; color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.09em;
}
.jersey {
  font-size: 24px; font-weight: 800; color: var(--brand);
  font-variant-numeric: tabular-nums; letter-spacing: -0.03em;
}
.ruled-out {
  padding: 2px 7px; background: var(--sev-critical); color: #fff;
  font-size: 9px; font-weight: 800;
  text-transform: uppercase; letter-spacing: 0.1em;
}

.conditions { font-size: 11.5px; color: var(--muted); }
.conditions b { color: var(--ink); font-weight: 600; }

.flags { display: flex; flex-direction: column; gap: 6px; }
.flag {
  display: grid; grid-template-columns: 3px 1fr; gap: 9px;
  font-size: 12px; line-height: 1.4;
}
.flag::before { content: ""; background: var(--flag-color); }
.flag-code {
  display: block; font-size: 9.5px; color: var(--flag-color);
  text-transform: uppercase; letter-spacing: 0.09em; font-weight: 800;
}
.quiet { margin: 0; font-size: 11.5px; color: var(--dim); font-style: italic; }

.price {
  display: flex; align-items: baseline; gap: 10px;
  padding: 7px 10px; background: var(--surface-2);
  font-size: 12.5px; font-variant-numeric: tabular-nums;
}
.price .salary { font-weight: 700; }
.price .proj { color: var(--muted); }
.price .value { margin-left: auto; font-weight: 700; color: var(--brand); }

.issues { display: flex; flex-direction: column; gap: 4px; }
.issue { margin: 0; font-size: 11px; line-height: 1.4; color: var(--muted); }
.issue b {
  color: var(--issue-color); font-weight: 800;
  text-transform: uppercase; letter-spacing: 0.07em; font-size: 9.5px;
}

/* Platform scoping: only the selected platform's rows render. Scoped to the
   grid so the toggle's own buttons are never hidden by it. */
:root[data-platform="draftkings"] .grid [data-platform="fanduel"],
:root[data-platform="fanduel"] .grid [data-platform="draftkings"] { display: none; }

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
        return '<p class="quiet">No flags.</p>'
    rows = []
    for flag in flags:
        color = SEVERITY_COLORS.get(flag["severity"], "var(--sev-info)")
        scope = f' data-platform="{flag["platform"]}"' if flag.get("platform") else ""
        rows.append(
            f'<div class="flag"{scope} style="--flag-color:{color}">'
            f'<span><span class="flag-code">{_esc(flag["code"])}</span>'
            f'{_esc(flag["reason"])}</span></div>'
        )
    return f'<div class="flags">{"".join(rows)}</div>'


def _render_pricing(pricing: list[dict]) -> str:
    """One row per platform. The toggle decides which is visible."""
    if not pricing:
        return '<p class="quiet">No salary loaded.</p>'

    rows = []
    for entry in pricing:
        rows.append(
            f'<div class="price" data-platform="{_esc(entry["platform"])}">'
            f'<span class="salary">${entry["salary"]:,}</span>'
            f'<span class="proj">{entry["projected"]:.1f} proj</span>'
            f'<span class="value">{entry["value"]:.2f}<span class="proj"> /$1k</span></span>'
            f"</div>"
        )
    return "".join(rows)


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


def _render_player(row: dict) -> str:
    badge = '<span class="ruled-out">Out</span>' if not row["playable"] else ""
    conditions = (
        f'<p class="conditions">{_esc(row["matchup"])} · <b>{_esc(row["conditions"])}</b></p>'
        if row["conditions"]
        else f'<p class="conditions">{_esc(row["matchup"])}</p>'
    )
    return (
        f'<div class="player" data-playable="{str(row["playable"]).lower()}">'
        f'<div class="identity">'
        f'<div class="avatar">{_esc(row["initials"])}</div>'
        f'<div class="who"><h3 class="name">{_esc(row["name"])} {badge}</h3>'
        f'<div class="slot">{_esc(row["team"])}</div></div>'
        f'<div class="jersey">{_esc(row["number"])}</div>'
        f"</div>"
        f"{conditions}"
        f'{_render_flags(row["flags"])}'
        f'{_render_pricing(row["pricing"])}'
        f'{_render_issues(row["issues"])}'
        f"</div>"
    )


def _render_card(card: dict) -> str:
    if not card["players"]:
        body = '<div class="player"><p class="quiet">No players at this position.</p></div>'
    else:
        body = "".join(_render_player(row) for row in card["players"])

    return (
        f'<article class="card" style="--card-accent:{_esc(card["accent"])}">'
        f'<div class="card-head">'
        f'<h2 class="card-title">{_esc(card["label"])}</h2>'
        f'<span class="card-count">{len(card["players"])}</span>'
        f"</div>"
        f"{body}"
        f"</article>"
    )


def render(view: dict) -> str:
    """Return the full page for a view model from `manager.build_view()`."""
    scoring = view["scoring"]
    platforms = view.get("platforms") or sorted(scoring)
    default_platform = platforms[0]

    buttons = "".join(
        f'<button type="button" data-target="{_esc(p)}" '
        f'aria-pressed="{str(p == default_platform).lower()}">'
        f"{PLATFORM_LABELS.get(p, p[:2].upper())}</button>"
        for p in platforms
    )
    caps = {
        p: f"${scoring[p]['salary_cap']:,} cap · {scoring[p]['points_per_reception']} PPR"
        for p in platforms
        if p in scoring
    }

    return f"""<title>Fantasy Bet Helper</title>
<style>{CSS}</style>
<div class="shell">
  <header class="masthead">
    <h1 class="wordmark">Fantasy<span>&nbsp;Bet</span><br>Helper</h1>
    <div class="masthead-right">
      <div class="masthead-meta">
        <span>Splits <b>{view["splits_season"]}</b></span>
        <span>Generated <b>{_esc(view["generated_at"][:16].replace("T", " "))} UTC</b></span>
      </div>
      <div class="toggle" role="group" aria-label="Sportsbook">{buttons}</div>
      <p class="toggle-cap" id="cap"></p>
    </div>
  </header>

  <main class="grid">{"".join(_render_card(c) for c in view["cards"])}</main>

  <footer class="colophon">
    Weather from the National Weather Service. Splits, rosters, and injury
    designations from nflverse. Salaries from DraftKings and FanDuel contest
    exports; projections fall back to each export's season average when
    <code>ODDS_API_KEY</code> is unset. Manual corrections in
    <code>overrides.json</code> always take precedence over upstream data.
  </footer>
</div>
<script>
  var CAPS = {json.dumps(caps)};
  var root = document.documentElement;
  var cap = document.getElementById("cap");
  var buttons = document.querySelectorAll(".toggle button");

  function select(platform) {{
    root.setAttribute("data-platform", platform);
    buttons.forEach(function (b) {{
      b.setAttribute("aria-pressed", String(b.dataset.target === platform));
    }});
    cap.textContent = CAPS[platform] || "";
  }}

  buttons.forEach(function (b) {{
    b.addEventListener("click", function () {{ select(b.dataset.target); }});
  }});
  select({json.dumps(default_platform)});
</script>
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
