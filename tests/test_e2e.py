"""Offline end to end: the daemon loop with fake SEC and Alpaca, then every read-only view.

poll -> bars -> observe -> settle -> brief, then the web page, an MCP tools/call and a ledger
verify. The clock is fake and in the past; nothing leaves the process.
"""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

from jevtrader import app, bars, daemon, feeds, mcp_server, paths, sec, secrets, web
from jevtrader import config as settings
from jevtrader.cli import main
from jevtrader.common import instant, load_strategy, timestamp
from jevtrader.store import Ledger

ET = ZoneInfo("America/New_York")
UA = "Test Person test@example.com"
CIK, TICKER = "0000000123", "ABC"
ACCESSION = "0000000123-26-000001"
ARCHIVE = f"https://www.sec.gov/Archives/edgar/data/123/{ACCESSION.replace('-', '')}/"
EVENT_ID = f"sec:{ACCESSION}:ex99-1.htm"
UNQUOTABLE = "ZQX-UNIQUE-TAIL-7781"
EXHIBIT = (
    "<html><body><p>ABC Corp raised guidance for fiscal 2026, citing strong demand across "
    "every region it serves.</p><p>Record revenue was reported for the quarter.</p>"
    f"<p>This forward-looking statement is subject to risks {UNQUOTABLE}.</p></body></html>"
)
FIRST = "2026-03-10T14:00:00Z"  # Tuesday 10:00 in New York
LATER = "2026-03-31T21:00:00Z"  # three weeks on, Tuesday 17:00: the horizon has matured
FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb="
    "&owner=include&start=0&count=100&output=atom"
)


def atom() -> str:
    summary = f"<b>Filed:</b> 2026-03-10 <b>AccNo:</b> {ACCESSION}"
    summary += "<br>Item 8.01: Other Events<br>Item 9.01: Financial Statements and Exhibits"
    return (
        '<?xml version="1.0" encoding="ISO-8859-1" ?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>Latest Filings</title>'
        f"<entry><title>8-K - ABC Corp ({CIK}) (Filer)</title>"
        f'<summary type="html">{escape(summary)}</summary>'
        "<updated>2026-03-10T08:05:00-04:00</updated>"
        '<category scheme="https://www.sec.gov/" label="form type" term="8-K"/>'
        f"<id>urn:tag:sec.gov,2008:accession-number={ACCESSION}</id></entry></feed>"
    )


def form_index() -> str:
    rows = [("8-K", "ABC Corp", 123, ACCESSION), ("8-K", "Other Inc", 456, "0000000456-26-000009")]
    lines = [
        f"{form:<12}{name:<62}{cik:<12}20260309    edgar/data/{cik}/{number}.txt"
        for form, name, cik, number in rows
    ]
    return (
        "Form Type   Company Name   CIK   Date Filed  File Name\n"
        + "-" * 80
        + "\n"
        + ("\n".join(lines) + "\n")
    )


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url


class FakeSEC:
    """Every approved SEC URL the run needs; anything else fails the test."""

    def __init__(self):
        self.urls: list[str] = []
        self.agents: set[str] = set()
        self.responses = {
            FEED: atom(),
            "https://www.sec.gov/files/company_tickers.json": {
                "0": {"cik_str": 123, "ticker": TICKER, "title": "ABC Corp"}
            },
            f"https://data.sec.gov/submissions/CIK{CIK}.json": {
                "cik": "123",
                "filings": {
                    "recent": {
                        "accessionNumber": [ACCESSION],
                        "form": ["8-K"],
                        "primaryDocument": ["abc-8k.htm"],
                        "acceptanceDateTime": ["2026-03-10T08:05:00.000Z"],
                        "items": ["8.01,9.01"],
                    }
                },
            },
            ARCHIVE + "index.json": {
                "directory": {"item": [{"name": "abc-8k.htm"}, {"name": "ex99-1.htm"}]}
            },
            ARCHIVE + "ex99-1.htm": EXHIBIT,
        }

    def __call__(self, request, *, timeout):
        url = request.full_url
        self.urls.append(url)
        self.agents.add(request.get_header("User-agent"))
        if "/daily-index/" in url:
            return Response(form_index().encode("latin-1"), url)
        if url not in self.responses:
            raise AssertionError(f"Unexpected SEC request: {url}")
        value = self.responses[url]
        body = value if isinstance(value, str) else json.dumps(value)
        return Response(body.encode(), url)


def weekdays(start: date, end: date) -> list[date]:
    days = []
    while start <= end:
        if start.weekday() < 5:
            days.append(start)
        start += timedelta(days=1)
    return days


class FakeAlpaca:
    """Weekday sessions 09:30-16:00 New York; raw bars that drift upward; no corporate actions."""

    def __init__(self):
        self.paths: list[str] = []
        self.keys: set[str] = set()

    def __call__(self, url, headers, timeout):
        parts = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        self.paths.append(parts.path)
        self.keys.add(headers["APCA-API-KEY-ID"])
        if parts.path == "/v2/calendar":
            days = weekdays(date.fromisoformat(query["start"]), date.fromisoformat(query["end"]))
            rows = [{"date": d.isoformat(), "open": "09:30", "close": "16:00"} for d in days]
            return json.dumps(rows).encode()
        if parts.path == "/v1/corporate-actions":
            return json.dumps({"corporate_actions": {}, "next_page_token": None}).encode()
        if parts.path == "/v2/stocks/bars":
            first = instant(query["start"]).astimezone(ET).date() + timedelta(days=1)
            last = instant(query["end"]).astimezone(ET).date()
            result = {
                name: [self.bar(name, d) for d in weekdays(first, last)]
                for name in (query["symbols"].split(","))
            }
            return json.dumps({"bars": result, "next_page_token": None}).encode()
        raise AssertionError(f"Unexpected Alpaca request: {url}")

    @staticmethod
    def bar(name: str, day: date) -> dict:
        step = (day - date(2025, 10, 1)).days
        price = 500 + 0.5 * step if name == "SPY" else 40 + 0.2 * step + (step % 3) * 0.1
        midnight = datetime(day.year, day.month, day.day, tzinfo=ET).astimezone(timezone.utc)
        return {
            "t": midnight.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "o": price,
            "h": price + 1,
            "l": price - 1,
            "c": price + 0.25,
            "v": 50_000_000 if name == "SPY" else 2_000_000,
        }


class Clock:
    def __init__(self, now: str):
        self.now = timestamp(now)

    def __call__(self) -> str:
        return self.now

    def moment(self) -> datetime:
        return instant(self.now)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.clock = Clock(FIRST)
        self.sec, self.alpaca = FakeSEC(), FakeAlpaca()
        self.notes: list[tuple[str, str]] = []
        quiet = sec._RateLimiter(lambda: 0.0, lambda _: None)
        for patcher in (
            patch.dict(
                os.environ,
                {
                    paths.HOME_ENV: str(self.dir / "home"),
                    "ALPACA_API_KEY_ID": "test-key-id",
                    "ALPACA_API_SECRET_KEY": "test-secret-key",
                },
            ),
            patch.object(secrets, "keychain_available", return_value=False),
            patch.object(sec, "_DEFAULT_LIMITER", quiet),
            patch.object(bars, "_LIMITER", bars._RateLimiter(lambda: 0.0, lambda _: None)),
            patch.object(sec, "utc_now", self.clock.moment),
            # Every module that stamps receipt or decision time reads the same fake clock.
            patch("jevtrader.engine.utc_now", self.clock),
            patch("jevtrader.market.utc_now", self.clock),
            patch("jevtrader.bars.utc_now", self.clock),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = settings.validate(
            {
                "sec_user_agent": UA,
                "watchlist": [TICKER],
                "bars_source": "alpaca",
                "ledger": str(self.dir / "forward.sqlite"),
            }
        )
        settings.save(self.config)
        self.ledger_path = str(settings.ledger_path(self.config))
        Ledger(self.ledger_path).close()  # what `setup` does
        self.strategy = load_strategy()

    def run_service(self, now: str) -> list[dict]:
        """One daemon start: gap check, one tick of due jobs, then a clean stop."""
        self.clock.now = timestamp(now)
        stop = threading.Event()
        with Ledger(self.ledger_path, create=False) as ledger:
            before = {r["id"] for r in ledger.all("runs")}
            ctx = app.daemon_context(
                ledger,
                self.config,
                self.strategy,
                clock=self.clock,
                poll=lambda *a, **k: feeds.poll(
                    *a, **k, transport=self.sec, memory=feeds.PollMemory()
                ),
                bars=lambda *a, **k: bars.fetch_forward(*a, **k, transport=self.alpaca),
                reconcile=lambda *a, **k: feeds.daily_index(*a, **k, transport=self.sec),
                notify=lambda title, body: self.notes.append((title, body)) or True,
                log=lambda line: None,
            )
            daemon.run_forever(
                ctx, stop=stop, sleep=lambda _: stop.set(), lock_path=self.dir / "daemon.lock"
            )
            runs = [r for r in ledger.all("runs") if r["id"] not in before]
        return sorted(runs, key=lambda r: (r["started_at"], r["job"]))

    def statuses(self, runs: list[dict]) -> dict:
        return {run["job"]: run["status"] for run in runs}

    def mcp(self, *calls: tuple[str, dict]) -> list[dict]:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ] + [
            {
                "jsonrpc": "2.0",
                "id": n,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            for n, (name, arguments) in enumerate(calls, 1)
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        handlers = app.mcp_handlers(self.ledger_path, self.config, clock=self.clock)
        mcp_server.serve(
            handlers,
            stdin=io.StringIO("".join(json.dumps(m) + "\n" for m in messages)),
            stdout=stdout,
            stderr=stderr,
        )
        self.mcp_output = stdout.getvalue()
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], list(range(len(calls) + 1)))
        return [reply["result"] for reply in replies[1:]]

    def test_daemon_loop_to_web_mcp_and_verify(self):
        # Day one, 10:00 New York: every job is due on a fresh ledger.
        first = self.run_service(FIRST)
        self.assertEqual(
            self.statuses(first),
            {job: "ok" for job in ("poll", "bars", "observe", "settle", "brief", "reconcile")},
            [(r["job"], r["error"]) for r in first],
        )
        by_job = {run["job"]: run["counts"] for run in first}
        self.assertEqual(by_job["poll"]["added"], 1)
        self.assertGreaterEqual(by_job["bars"]["added"], 2 * 21)
        self.assertEqual(by_job["observe"]["forecasts"], 1)
        self.assertEqual(by_job["settle"], {"added": 0, "unresolved": 1})
        self.assertEqual((by_job["brief"]["filings"], by_job["brief"]["notified"]), (1, 1))
        self.assertEqual((by_job["reconcile"]["indexed"], by_job["reconcile"]["collected"]), (2, 1))
        self.assertEqual(self.sec.agents, {UA})
        self.assertEqual(self.alpaca.keys, {"test-key-id"})
        title, body = self.notes[0]
        self.assertIn("1 new filing", title)
        self.assertIn("ABC WATCH", body)
        self.assertNotIn(UNQUOTABLE, title + body)

        with Ledger(self.ledger_path, readonly=True) as ledger:
            event = ledger.get("disclosures", EVENT_ID)
            [forecast] = ledger.all("forecasts")
        self.assertEqual((event["mode"], event["first_seen_at"]), ("forward", self.clock.now))
        self.assertEqual(
            (forecast["mode"], forecast["eligibility"], forecast["action"]),
            ("forward", "forward", "WATCH"),
        )
        self.assertEqual(forecast["decision_at"], timestamp(FIRST))
        head = self.head()

        # The page an hour later shows the filing with its code-computed decision.
        status, headers, page = web.respond(
            self.ledger_path,
            "GET",
            "/",
            ["127.0.0.1:8765"],
            port=8765,
            now=timestamp("2026-03-10T15:00:00Z"),
            watchlist=[TICKER],
        )
        html = page.decode()
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("Research tool, not investment advice.", html)
        self.assertIn(">ABC<", html)
        self.assertIn("WATCH", html)
        self.assertIn("raised guidance", html)
        self.assertNotIn(UNQUOTABLE, html)
        self.assertEqual(self.head(), head)  # the page wrote nothing

        # Three weeks later the service restarts: a coverage gap is recorded honestly,
        # the new sessions arrive and the WATCH decision gets its matured label.
        later = self.run_service(LATER)
        self.assertEqual(self.statuses(later)["coverage_gap"], "gap")
        self.assertTrue(
            all(run["status"] == "ok" for run in later if run["job"] != "coverage_gap"),
            [(r["job"], r["error"]) for r in later],
        )
        counts = {run["job"]: run["counts"] for run in later}
        self.assertEqual(counts["poll"]["new"], 0)
        self.assertEqual(counts["settle"], {"added": 1, "unresolved": 0})
        self.assertEqual(counts["brief"]["filings"], 0)
        with Ledger(self.ledger_path, readonly=True) as ledger:
            [outcome] = ledger.all("outcomes")
        self.assertEqual(outcome["forecast_id"], forecast["id"])
        self.assertLessEqual(instant(outcome["label_available_at"]), instant(LATER))

        status, _, page = web.respond(
            self.ledger_path,
            "GET",
            f"/filing/{quote(EVENT_ID, safe='')}",
            ["localhost:8765"],
            port=8765,
            now=self.clock.now,
            watchlist=[TICKER],
        )
        self.assertEqual(status, 200)
        # A WATCH made no call: the outcome is the stock's move, never shown as a result.
        self.assertIn("stock minus SPY over the label window (no position taken)", page.decode())
        self.assertNotIn("benchmark-relative return", page.decode())
        status, _, page = web.respond(
            self.ledger_path,
            "GET",
            "/health",
            ["127.0.0.1:8765"],
            port=8765,
            now=self.clock.now,
        )
        self.assertIn("coverage_gap", page.decode())

        # The MCP tools read the same ledger: cards, numbers and labels, never raw text.
        search, card, report, health, today = self.mcp(
            ("search_filings", {"symbol": "abc"}),
            ("explain_filing", {"event_id": EVENT_ID}),
            ("evidence_report", {}),
            ("health", {}),
            ("today_brief", {}),
        )
        self.assertNotIn(UNQUOTABLE, self.mcp_output)
        for result in (search, card, report, health, today):
            self.assertNotIn("isError", result, result)
        [found] = search["structuredContent"]["filings"]
        self.assertEqual((found["event_id"], found["action"]), (EVENT_ID, "WATCH"))
        # Filing text leaves only through explain_filing.
        self.assertFalse({"quotes", mcp_server.EXCERPT_KEY} & set(found), found)
        excerpts = card["structuredContent"][mcp_server.EXCERPT_KEY]
        self.assertTrue(excerpts)
        self.assertLessEqual(sum(map(len, excerpts)), mcp_server.MAX_QUOTE_CHARS)
        self.assertEqual(card["structuredContent"]["excerpt_note"], mcp_server.EXCERPT_NOTE)
        decision = card["structuredContent"]["decisions"][0]
        self.assertEqual(decision["evidence"], "forward")
        self.assertIsNotNone(decision["outcome"])
        self.assertIsNone(decision["outcome"]["net_return"])  # WATCH is never a call
        board = report["structuredContent"]
        self.assertEqual((board["status"], board["calls"]), ("collecting", 0))
        self.assertEqual(board["baseline"]["events"], 1)
        self.assertTrue(health["structuredContent"]["ledger"]["ok"])
        self.assertIn("coverage_gap", health["structuredContent"]["jobs"])
        self.assertEqual(today["structuredContent"]["total"], 0)  # nothing new in 3 days

        # The chain covers every record the loop wrote, from the CLI too.
        with Ledger(self.ledger_path, readonly=True) as ledger:
            report = ledger.verify(anchor=head)
        self.assertTrue(report["ok"], report["problems"])
        self.assertGreater(report["chain_length"], head["seq"])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["verify"]), 0)
        verified = json.loads(stdout.getvalue())
        self.assertEqual((verified["ok"], verified["ledger"]), (True, self.ledger_path))

    def head(self) -> dict:
        with Ledger(self.ledger_path, readonly=True) as ledger:
            return ledger.head()


if __name__ == "__main__":
    unittest.main()
