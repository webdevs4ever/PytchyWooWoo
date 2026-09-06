"""Local web server for Comp Mode.

Serves an upload form, runs the decisioning engine on whatever lineup you drop
in, and renders the marked result. The logic stays in Python — this is a view
over `sources/comp.py`, not a second implementation of it.

Binds to localhost only. Nothing here is hardened for exposure to a network,
and it does not need to be: it exists so a browser can hand a file to code that
already works.

    python -m sources.serve            # http://127.0.0.1:8765
    python -m sources.serve --port 9000
"""

from __future__ import annotations

if __name__ == "__main__" and __package__ in (None, ""):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "sources"

import html
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import config
from models import Platform
from sources.comp import (
    MARK_ACTIVE,
    MARK_OUT,
    MARK_QUESTIONABLE,
    MARK_STORY,
    CompUnavailable,
    build_user_lineup,
    compare,
    mark_lineup,
    optimize,
    parse_lineup,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765

STATUS_TONE = {"active": "ok", "questionable": "warn", "out": "bad"}

CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --ground:#0a0a0c;--surface:#17171c;--surface-2:#1f1f27;--edge:#2c2c36;
  --ink:#f4f2ef;--muted:#8d8a95;--dim:#66636e;--brand:#f0522c;--gold:#c8a15a;
  --ok:#4f9d6a;--warn:#e8a33d;--bad:#ff5a36;
}
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]){
  --ground:#edecea;--surface:#fff;--surface-2:#f4f3f1;--edge:#ddd9d4;
  --ink:#1a181c;--muted:#6b6772;--dim:#928d99;--brand:#c93d18;--gold:#9a7738;
  --ok:#2f7048;--warn:#9a6c12;--bad:#cc3d1c;
}}
body{margin:0;background:var(--ground);color:var(--ink);
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
.shell{max-width:940px;margin:0 auto;padding:40px 24px 72px}
h1{margin:0 0 4px;font-size:clamp(24px,4vw,34px);font-weight:800;
  text-transform:uppercase;letter-spacing:-0.02em}
h1 span{color:var(--brand)}
.sub{margin:0 0 28px;color:var(--muted);font-size:13px}
form{display:flex;flex-direction:column;gap:16px;padding:22px;
  background:var(--surface);border:1px solid var(--edge);
  border-top:3px solid var(--brand)}
label{font-size:11px;font-weight:800;text-transform:uppercase;
  letter-spacing:0.1em;color:var(--muted)}
textarea,input[type=file],select{width:100%;padding:10px;font:inherit;
  background:var(--surface-2);color:var(--ink);border:1px solid var(--edge)}
textarea{min-height:150px;font-family:ui-monospace,Menlo,monospace;font-size:13px}
.row{display:flex;gap:16px;flex-wrap:wrap}
.row>div{flex:1;min-width:200px;display:flex;flex-direction:column;gap:6px}
button{align-self:flex-start;padding:11px 26px;border:0;cursor:pointer;
  background:var(--brand);color:#fff;font:inherit;font-weight:800;
  text-transform:uppercase;letter-spacing:0.09em;font-size:12px}
button:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
.compare{display:grid;gap:18px;margin-top:26px;
  grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
.card{background:var(--surface);border:1px solid var(--edge);
  border-top:3px solid var(--card-accent,var(--edge))}
.card h2{margin:0;padding:15px 18px;border-bottom:1px solid var(--edge);
  font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:0.1em}
.tot{font-weight:400;text-transform:none;letter-spacing:0;color:var(--muted);
  font-size:12px;margin-left:8px;font-variant-numeric:tabular-nums}
.slotrow{border-top:1px solid var(--edge)}
.slotrow:first-of-type{border-top:0}
.slotrow>summary{display:grid;grid-template-columns:60px 46px 1fr auto;gap:10px;
  align-items:baseline;padding:10px 16px;font-size:13px;cursor:pointer;
  font-variant-numeric:tabular-nums;list-style:none}
.slotrow>summary::-webkit-details-marker{display:none}
.slotrow>summary:hover{background:var(--surface-2)}
.slotrow>summary:focus-visible{outline:2px solid var(--gold);outline-offset:-2px}
.mk{font-size:15px;letter-spacing:2px}
.mk[data-tone=ok]{color:var(--ok)}
.mk[data-tone=warn]{color:var(--warn)}
.mk[data-tone=bad]{color:var(--bad)}
.why{margin:0;padding:2px 16px 14px 76px;background:var(--surface-2);
  font-size:11.5px;line-height:1.55;color:var(--muted)}
.why li{margin-bottom:6px}
.hint{font-size:11px;color:var(--dim);padding:10px 16px 0}
.slot{font-size:10.5px;color:var(--muted);text-transform:uppercase;
  letter-spacing:0.09em}
.who b{font-weight:700}
.note{display:block;font-size:11.5px;color:var(--muted);font-style:italic}
.legend{margin-top:18px;font-size:12px;color:var(--muted);display:flex;
  gap:18px;flex-wrap:wrap}
.swaps{margin-top:26px;padding:18px;background:var(--surface);
  border:1px solid var(--edge)}
.swaps h2{margin:0 0 12px;padding:0;border:0;font-size:13px;font-weight:800;
  text-transform:uppercase;letter-spacing:0.1em}
.swap{padding:8px 0;font-size:13px;font-variant-numeric:tabular-nums}
.swap+.swap{border-top:1px solid var(--edge)}
.swap i{color:var(--muted);font-size:11.5px;display:block}
.err{margin-top:26px;padding:16px 18px;background:var(--surface);
  border:1px solid var(--bad);border-left:3px solid var(--bad);font-size:13.5px}
"""


def _page(body: str) -> bytes:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Comp Mode</title><style>{CSS}</style></head><body>
<div class="shell">
<h1>Comp<span> Mode</span></h1>
<p class="sub">Upload a lineup. The decisioning engine marks each player, then
compares your lineup against the optimal one.</p>
{body}
</div></body></html>""".encode("utf-8")


def _form(platform: str = Platform.DRAFTKINGS.value) -> str:
    options = "".join(
        f'<option value="{p.value}"{" selected" if p.value == platform else ""}>'
        f"{p.value.title()}</option>"
        for p in Platform
    )
    return f"""<form method="post" action="/analyze" enctype="multipart/form-data">
  <div class="row">
    <div><label for="file">Lineup file</label>
      <input type="file" id="file" name="file" accept=".txt,.csv"></div>
    <div><label for="platform">Sportsbook</label>
      <select id="platform" name="platform">{options}</select></div>
  </div>
  <div><label for="text">…or paste names, one per line</label>
    <textarea id="text" name="text" placeholder="Patrick Mahomes&#10;Chuba Hubbard&#10;…"></textarea></div>
  <button type="submit">Analyze lineup</button>
</form>"""


def _render_lineup(marked, title: str, cap: int, accent: str) -> str:
    points = round(sum(m.entry.projected_points for m in marked), 2)
    salary = sum(m.entry.salary for m in marked)
    rows = []
    for mark in marked:
        entry = mark.entry
        tone = STATUS_TONE.get(mark.status, "ok")
        note = f'<span class="note">{html.escape(mark.note)}</span>' if mark.note else ""
        # <details> keeps the reveal keyboard-accessible and needs no script.
        why = "".join(f"<li>{html.escape(line)}</li>" for line in mark.logic)
        rows.append(
            f'<details class="slotrow"><summary>'
            f'<span class="mk" data-tone="{tone}">{mark.markers}</span>'
            f'<span class="slot">{html.escape(entry.slot)}</span>'
            f'<span class="who"><b>{html.escape(entry.player.name)}</b> '
            f'<span class="slot">{html.escape(entry.player.team)}</span>{note}</span>'
            f"<span>{entry.projected_points:.1f}</span>"
            f'</summary><ul class="why">{why}</ul></details>'
        )
    return (
        f'<section class="card" style="--card-accent:{accent}">'
        f"<h2>{html.escape(title)}<span class=\"tot\">{points} proj · "
        f"${salary:,} of ${cap:,}</span></h2>"
        f'<p class="hint">Click any row to see why its marker was assigned.</p>'
        f'{"".join(rows)}</section>'
    )


def _render_result(comparison, user_marked, optimal_marked, rules) -> str:
    parts = [
        '<div class="compare">'
        + _render_lineup(user_marked, "Your lineup", rules.salary_cap, "var(--gold)")
        + _render_lineup(optimal_marked, "Optimized", rules.salary_cap, "var(--brand)")
        + "</div>",
        f'<p class="legend"><span>{MARK_ACTIVE} active</span>'
        f"<span>{MARK_QUESTIONABLE} questionable</span>"
        f"<span>{MARK_OUT} ruled out</span>"
        f"<span>{MARK_STORY} narrative street</span></p>",
    ]

    if comparison.missing:
        parts.append(
            '<div class="err"><b>Not found in the salary export:</b> '
            + html.escape(", ".join(comparison.missing))
            + "</div>"
        )
    if comparison.over_cap:
        parts.append(
            f'<div class="err">Over the cap by ${comparison.over_cap:,} — '
            "this lineup is invalid.</div>"
        )

    if comparison.swaps:
        swaps = []
        for swap in comparison.swaps:
            sign = "+" if swap.salary_delta >= 0 else "−"
            note = f"<i>{html.escape(swap.note)}</i>" if swap.note else ""
            swaps.append(
                f'<div class="swap"><b>{html.escape(swap.out_player.name)}</b> → '
                f"<b>{html.escape(swap.in_player.name)}</b> "
                f"{swap.points_delta:+.1f} pts · {sign}${abs(swap.salary_delta):,}"
                f"{note}</div>"
            )
        parts.append(
            f'<section class="swaps"><h2>Suggested swaps '
            f"({comparison.points_gap:+} projected points)</h2>"
            f'{"".join(swaps)}</section>'
        )
    else:
        parts.append(
            '<section class="swaps"><h2>No swaps</h2>'
            "<p>Your lineup is already optimal.</p></section>"
        )

    return "".join(parts)


def _parse_body(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    """Read a form post: multipart when a file is attached, urlencoded otherwise."""
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b""
    content_type = handler.headers.get("Content-Type", "")

    if not content_type.startswith("multipart/form-data"):
        return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}

    match = re.search(r"boundary=(.+)$", content_type)
    if not match:
        return {}

    boundary = ("--" + match.group(1).strip('"')).encode()
    fields: dict[str, str] = {}
    for chunk in raw.split(boundary):
        if b"\r\n\r\n" not in chunk:
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]*)"', head)
        if not name:
            continue
        value = data.rstrip(b"\r\n--").decode("utf-8", "replace")
        key = name.group(1).decode()
        # A file input submits empty when nothing is chosen; keep the textarea
        # value in that case rather than overwriting it with "".
        if value or key not in fields:
            fields[key] = value
    return fields


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/", "/index.html"):
            self._send(_page('<div class="err">Not found.</div>'), 404)
            return
        self._send(_page(_form()))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/analyze":
            self._send(_page('<div class="err">Not found.</div>'), 404)
            return

        fields = _parse_body(self)
        platform = Platform(fields.get("platform") or Platform.DRAFTKINGS.value)
        text = fields.get("file") or fields.get("text") or ""

        names = parse_lineup(text)
        if not names:
            self._send(
                _page(
                    _form(platform.value)
                    + '<div class="err">No player names found. Attach a file or '
                    "paste one name per line.</div>"
                )
            )
            return

        from sources.odds import get_book

        rules = config.LINEUPS[platform]
        pool = get_book(use_props=False).pool_for(platform)
        if not pool:
            self._send(
                _page(
                    _form(platform.value)
                    + f'<div class="err">No {platform.value} salary export in '
                    f"{config.SALARY_DIR}/ — see its README.</div>"
                )
            )
            return

        try:
            optimal = optimize(pool, rules)
        except CompUnavailable as exc:
            self._send(
                _page(_form(platform.value) + f'<div class="err">{html.escape(str(exc))}</div>')
            )
            return

        user, missing = build_user_lineup(names, pool, rules)
        comparison = compare(user, optimal, missing)
        body = _form(platform.value) + _render_result(
            comparison, mark_lineup(user), mark_lineup(optimal), rules
        )
        self._send(_page(body))

    def log_message(self, fmt: str, *args) -> None:
        pass  # the default logger writes a line per asset request; too noisy


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Serve Comp Mode locally.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    server = HTTPServer((HOST, args.port), Handler)
    print(f"Comp Mode on http://{HOST}:{args.port}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
