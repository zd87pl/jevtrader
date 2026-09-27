"""Read-only localhost pages over the ledger.

Binds loopback only and answers only ``Host: 127.0.0.1:<port>`` or ``localhost:<port>``,
so a rebinding DNS name cannot read it. GET/HEAD only, no forms, strict CSP, every value
escaped, and the ledger is opened read-only per request. Every page states that this is a
research tool, not investment advice, and shows the evidence label.
"""

from __future__ import annotations

import html
import inspect
import json
import re
import sqlite3
import sys
import traceback
from collections.abc import Callable, Iterable
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from . import brief, evidence, paths
from .common import digest, instant, symbol, timestamp, utc_now
from .store import Ledger

DEFAULT_PORT = 8765
BIND_HOSTS = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1"}
LOOKBACK = timedelta(days=3)  # Covers a weekend, so Monday's page still shows Friday's filings.
MAX_TARGET_CHARS = 2_048
REQUEST_TIMEOUT_SECONDS = 30
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
HTML_TYPE = "text/html; charset=utf-8"
_FILING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,299}")
Response = tuple[int, dict[str, str], bytes]

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f5f1; --surface: #ffffff; --text: #20242a; --muted: #5f6670; --line: #e2dfd7;
  --accent: #2f5d8a; --long: #1f6b47; --short: #9b3d2f; --watch: #7a6414; --pass: #5f6670;
  --ok: #1f6b47; --bad: #9b3d2f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15171a; --surface: #1d2025; --text: #e7e5df; --muted: #a2a7ae; --line: #30343b;
    --accent: #93b8de; --long: #80c9a0; --short: #e89c8e; --watch: #d8c679; --pass: #a2a7ae;
    --ok: #80c9a0; --bad: #e89c8e;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
a { color: var(--accent); text-underline-offset: 2px; }
.top {
  display: flex; flex-wrap: wrap; gap: .5rem 1.25rem; align-items: baseline;
  max-width: 46rem; margin: 0 auto; padding: 1rem 1rem .25rem;
}
.brand { font-weight: 600; color: var(--text); text-decoration: none; }
.top nav { display: flex; gap: 1rem; font-size: .95rem; }
.notice {
  max-width: 46rem; margin: 0 auto; padding: .25rem 1rem .75rem; font-size: .875rem;
  color: var(--muted); border-bottom: 1px solid var(--line);
}
.notice .evidence { display: block; color: var(--text); }
main { max-width: 46rem; margin: 0 auto; padding: .5rem 1rem 2rem; }
h1 { font-size: 1.5rem; font-weight: 600; margin: 1rem 0 .25rem; }
h2 { font-size: 1.05rem; font-weight: 600; margin: 1.5rem 0 .5rem; }
h3 { font-size: .95rem; font-weight: 600; margin: 1rem 0 .25rem; }
.panel > h2:first-child, .card > h2:first-child { margin-top: 0; }
.meta, .muted { color: var(--muted); font-size: .9rem; }
.card, .panel {
  background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
  padding: .9rem 1rem; margin: .75rem 0;
}
.card.watch { border-left: 4px solid var(--accent); }
.card-top { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }
.sym { font-size: 1.15rem; font-weight: 650; letter-spacing: .02em; }
.pill, .tag {
  display: inline-block; font-size: .72rem; letter-spacing: .05em; text-transform: uppercase;
  border: 1px solid currentColor; border-radius: 999px; padding: 0 .5rem; line-height: 1.6;
}
.tag { color: var(--muted); text-transform: none; letter-spacing: 0; }
.a-long { color: var(--long); } .a-short { color: var(--short); }
.a-watch { color: var(--watch); } .a-pass, .a-unscored { color: var(--pass); }
.num { font-variant-numeric: tabular-nums; }
.reasons { margin: .25rem 0 .5rem; padding-left: 1.2rem; color: var(--muted); font-size: .9rem; }
.quote {
  margin: .5rem 0; padding: .1rem 0 .1rem .75rem; border-left: 3px solid var(--line);
  color: var(--text); font-size: .95rem; overflow-wrap: anywhere;
}
.links { font-size: .9rem; margin: .5rem 0 0; }
.jobs { list-style: none; padding: 0; margin: 0; }
.jobs li { padding: .15rem 0; }
.job { display: inline-block; min-width: 6rem; }
.st-ok { color: var(--ok); } .st-bad { color: var(--bad); font-weight: 600; }
.status { font-size: 1.25rem; font-weight: 600; margin: .25rem 0; }
.s-supported { color: var(--ok); } .s-no_edge { color: var(--bad); }
table { width: 100%; border-collapse: collapse; font-size: .92rem; }
th, td { text-align: left; padding: .35rem .25rem; border-bottom: 1px solid var(--line); }
th { font-weight: 500; color: var(--muted); }
.kv td { text-align: right; }
.scroll { overflow-x: auto; }
code { font-size: .8rem; overflow-wrap: anywhere; }
.foot { max-width: 46rem; margin: 0 auto; padding: 1rem; color: var(--muted); font-size: .8rem; }
@media (max-width: 480px) {
  body { font-size: 15px; }
  .card, .panel { padding: .75rem; border-radius: 10px; }
}
""".lstrip()


class ReadOnlyLedger:
    """Fallback reader for a store without ``readonly``: a mode=ro connection, hashes checked."""

    def __init__(self, path: str | Path):
        target = Path(path)
        if not target.is_file():
            raise ValueError(f"No ledger at {target}; run init first")
        self.db = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        self.db.execute("PRAGMA query_only=ON")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def _rows(self, sql: str, args: tuple) -> list[dict]:
        result = []
        for encoded, fingerprint in self.db.execute(sql, args).fetchall():
            value = json.loads(encoded)
            if digest(value) != fingerprint:
                raise ValueError(f"Corrupted record in {args[0]}")
            result.append(value)
        return result

    def get(self, kind: str, identity: str) -> dict | None:
        rows = self._rows(
            "SELECT payload, content_hash FROM records WHERE kind=? AND id=?", (kind, identity)
        )
        return rows[0] if rows else None

    def all(self, kind: str) -> list[dict]:
        return self._rows(
            "SELECT payload, content_hash FROM records WHERE kind=? ORDER BY id", (kind,)
        )

    def prefix(self, kind: str, prefix: str) -> list[dict]:
        return self._rows(
            "SELECT payload, content_hash FROM records WHERE kind=? AND id>=? AND id<? ORDER BY id",
            (kind, prefix, prefix + "￿"),
        )


def open_readonly(path: str | Path):
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"No ledger at {target}; run init first")
    if "readonly" in inspect.signature(Ledger).parameters:
        return Ledger(target, readonly=True)
    return ReadOnlyLedger(target)


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _plain(status: int, message: str) -> Response:
    headers = {**SECURITY_HEADERS, "Content-Type": "text/plain; charset=utf-8"}
    return status, headers, (message + "\n").encode()


def _json(value: object) -> Response:
    text = json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=True)
    # Belt and braces with nosniff: no markup-significant bytes even if sniffed.
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    headers = {**SECURITY_HEADERS, "Content-Type": "application/json; charset=utf-8"}
    return 200, headers, text.encode()


def page(title: str, body: str, evidence_label: str, *, status: int = 200) -> Response:
    """The shared layout: disclaimer and evidence label on every page."""
    document = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{_e(title)} · {_e(paths.APP_NAME)}</title>"
        '<link rel="stylesheet" href="/static/app.css"><link rel="icon" href="data:,">'
        "</head><body>"
        f'<header class="top"><a class="brand" href="/">{_e(paths.APP_NAME)}</a>'
        '<nav><a href="/">Brief</a><a href="/scoreboard">Scoreboard</a>'
        '<a href="/health">Health</a></nav></header>'
        f'<p class="notice"><strong>{_e(brief.NOTICE)}</strong> No orders are placed.'
        f' <span class="evidence">{_e(evidence_label)}</span></p>'
        f"<main>{body}</main>"
        '<footer class="foot">Numbers are computed by code from the local ledger. '
        "Read-only view; nothing here can change it.</footer>"
        "</body></html>\n"
    )
    return status, {**SECURITY_HEADERS, "Content-Type": HTML_TYPE}, document.encode()


def _message(title: str, text: str, evidence_label: str, status: int) -> Response:
    body = f'<h1>{_e(title)}</h1><p class="muted">{_e(text)}</p><p><a href="/">Back to the brief</a></p>'
    return page(title, body, evidence_label, status=status)


def _row(name: str, value: str) -> str:
    return f'<tr><th>{_e(name)}</th><td class="num">{_e(value)}</td></tr>'


STATUS_TEXT = {
    "collecting": "Too few matured calls to judge; the gate needs the minimum first.",
    "inconclusive": "Enough calls, but the interval includes both zero and a useful edge.",
    "supported": "The whole interval is above zero after costs. Still not investment advice.",
    "no_edge": "Even the top of the interval is below the futility threshold.",
}


def scoreboard_html(board: dict) -> str:
    gate, interval, counts = board["gate"], board["interval"], board["counts"]
    span = (
        f"{brief.percent(interval['low'])} to {brief.percent(interval['high'])}"
        if interval
        else "needs 2+ decision dates"
    )
    rows = [
        ("matured calls", f"{board['calls']} of {gate['min_matured_calls']} needed"),
        ("decision dates", str(board["call_dates"])),
        ("mean net return (per date)", brief.percent(board["mean_net_return"])),
        (f"{gate['confidence']:.0%} interval", span),
        ("calls with positive net", f"{board['positive_calls']} of {board['calls']}"),
        ("calls awaiting outcome", str(board["pending_calls"])),
        ("scored events", str(counts["scored_events"])),
        *((f"{action} decisions", str(counts[action])) for action in evidence.ACTIONS),
        (
            f"all-events mean target ({board['baseline']['events']} events)",
            brief.percent(board["baseline"]["mean_target"]),
        ),
        ("forecasts excluded (not evidence)", str(board["excluded_forecasts"])),
    ]
    meanings = "".join(
        f"<li><strong>{_e(name.replace('_', ' '))}</strong>: {_e(text)}</li>"
        for name, text in STATUS_TEXT.items()
    )
    gate_rows = [
        ("gate version", str(gate["version"])),
        ("minimum matured calls", str(gate["min_matured_calls"])),
        ("confidence", f"{gate['confidence']:.0%}"),
        ("futility upper bound", brief.percent(gate["futility_upper_bps"] / 10_000)),
    ]
    status = board["status"]
    return (
        "<h1>Scoreboard</h1>"
        f'<p class="status s-{_e(status)}">{_e(status.replace("_", " ").capitalize())}</p>'
        f"<p>{_e(board['label'])}</p>"
        f'<p class="meta">As of {_e(brief.et_time(board["as_of"]))}</p>'
        f'<div class="scroll"><table class="kv">{"".join(_row(k, v) for k, v in rows)}</table></div>'
        f"<h2>What the status means</h2><ul>{meanings}</ul>"
        f'<h2>Gate</h2><div class="scroll"><table class="kv">{"".join(_row(k, v) for k, v in gate_rows)}</table></div>'
        f'<p class="muted">Gate SHA-256 <code>{_e(board["gate_sha256"])}</code></p>'
        f'<p class="muted">Method: {_e(evidence.METHOD)}. Counts {_e(evidence.ELIGIBILITY_RULE)}.</p>'
    )


def _ago(minutes: int) -> str:
    if minutes < 90:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} days ago"


def _detail(run: dict) -> str:
    counts = ", ".join(f"{name} {value}" for name, value in sorted(run["counts"].items()))
    return " \u00b7 ".join(part for part in (run["error"], counts) if part)


def health_html(report: dict) -> str:
    if not report["jobs"]:
        table = '<p class="muted">No background runs recorded yet.</p>'
    else:
        rows = "".join(
            "<tr>"
            f"<td>{_e(job)}</td>"
            f'<td class="st-{"ok" if run["status"] in brief.OK_STATUSES else "bad"}">{_e(run["status"])}</td>'
            f'<td class="num">{_e(_ago(run["age_minutes"]))}</td>'
            f"<td>{_e(_detail(run))}</td>"
            "</tr>"
            for job, run in report["jobs"].items()
        )
        table = (
            '<div class="scroll"><table><tr><th>Job</th><th>Status</th><th>Last run</th>'
            f"<th>Detail</th></tr>{rows}</table></div>"
        )
    summary = {
        "ok": "All background jobs finished normally.",
        "attention": "Needs attention: " + ", ".join(report["attention"]) + ".",
        "idle": "The background service has not recorded a run yet.",
    }[report["state"]]
    return (
        f"<h1>Health</h1><p>{_e(summary)}</p>"
        f'<p class="meta">As of {_e(brief.et_time(report["as_of"]))}</p>{table}'
    )


def _route(ledger, path: str, now: str, watchlist: tuple[str, ...]) -> Response:
    if path in ("/", "/api/brief.json"):
        since = timestamp((instant(now) - LOOKBACK).isoformat())
        composed = brief.compose(ledger, now=now, since=since, watchlist=list(watchlist))
        if path != "/":
            return _json(composed)
        return page("Brief", brief.render_html(composed), composed["scoreboard"]["label"])
    board = evidence.scoreboard(ledger, as_of=now)
    if path == "/api/scoreboard.json":
        return _json(board)
    if path == "/scoreboard":
        return page("Scoreboard", scoreboard_html(board), board["label"])
    if path == "/health":
        return page("Health", health_html(brief.health(ledger, now=now)), board["label"])
    if path.startswith("/filing/"):
        event_id = unquote(path[len("/filing/") :], errors="strict")
        if _FILING_ID.fullmatch(event_id):
            try:
                card = brief.filing_card(ledger, event_id, now=now)
            except ValueError:
                pass
            else:
                return page(card["symbol"], brief.render_filing_html(card), board["label"])
        return _message("Filing not found", "No such filing is visible yet.", board["label"], 404)
    return _message("Not found", "There is no page at this address.", board["label"], 404)


def respond(
    ledger_path: str | Path,
    method: str,
    target: str,
    host_headers: Iterable[str],
    *,
    port: int,
    now: str,
    watchlist: Iterable[str] = (),
) -> Response:
    """One request to (status, headers, body); the ledger is opened only after every check."""
    hosts = [value.strip().lower() for value in host_headers]
    if len(hosts) != 1 or hosts[0] not in allowed_hosts(port):
        return _plain(403, "Forbidden: this server answers only 127.0.0.1 or localhost")
    if method not in ("GET", "HEAD"):
        status, headers, body = _plain(405, "Method not allowed: this server is read-only")
        return status, {**headers, "Allow": "GET, HEAD"}, body
    if len(target) > MAX_TARGET_CHARS or not target.startswith("/") or target.startswith("//"):
        return _plain(400, "Bad request target")
    path = target.split("#", 1)[0].split("?", 1)[0]
    if path == "/static/app.css":
        return 200, {**SECURITY_HEADERS, "Content-Type": "text/css; charset=utf-8"}, CSS.encode()
    symbols = tuple(symbol(name) for name in watchlist)
    try:
        ledger = open_readonly(ledger_path)
    except (ValueError, sqlite3.Error):
        return _message(
            "No ledger yet",
            "Run setup, or check the ledger path; nothing was created.",
            "Evidence: unavailable without a ledger",
            503,
        )
    with ledger:
        try:
            return _route(ledger, path, now, symbols)
        except UnicodeDecodeError:
            return _plain(400, "Bad request target")
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return _plain(500, "Internal error; see the server log")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    ledger_path: Path
    watchlist: tuple[str, ...]
    clock: Callable[[], str]
    quiet: bool


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = paths.APP_NAME
    sys_version = ""
    timeout = REQUEST_TIMEOUT_SECONDS  # An idle client cannot hold a thread open.

    def do_GET(self) -> None:
        self._reply()

    do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_GET

    def _reply(self) -> None:
        response = respond(
            self.server.ledger_path,
            self.command,
            self.path,
            self.headers.get_all("Host") or [],
            port=self.server.server_address[1],
            now=self.server.clock(),
            watchlist=self.server.watchlist,
        )
        self._write(*response)

    def _write(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None) -> None:
        # The stock error page carries no CSP; keep every response on the same headers.
        self.close_connection = True
        self._write(*_plain(int(code), "Bad request"))

    def log_message(self, format, *args) -> None:
        # The stock method escapes control characters and this override must too: a request
        # line is attacker text, and raw ESC or BEL would drive the terminal reading the log.
        if not self.server.quiet:
            line = "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in format % args)
            sys.stderr.write(f"{self.log_date_time_string()} {line}\n")


def make_server(
    ledger_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    watchlist: Iterable[str] = (),
    clock: Callable[[], str] = utc_now,
    quiet: bool = False,
) -> _Server:
    """Bound but not yet serving; port 0 picks a free port (see ``server_address``)."""
    if host not in BIND_HOSTS:
        raise ValueError("The web view binds loopback only: host must be 127.0.0.1 or localhost")
    if type(port) is not int or not 0 <= port <= 65_535:
        raise ValueError("port must be an integer from 0 to 65535")
    names = tuple(symbol(name) for name in watchlist)
    server = _Server((BIND_HOSTS[host], port), _Handler)
    server.ledger_path = Path(ledger_path)
    server.watchlist = names
    server.clock = clock
    server.quiet = quiet
    return server


def serve(
    ledger_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    watchlist: Iterable[str] = (),
    clock: Callable[[], str] = utc_now,
) -> None:
    server = make_server(ledger_path, host=host, port=port, watchlist=watchlist, clock=clock)
    print(f"Serving http://127.0.0.1:{server.server_address[1]}/ (read-only)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
