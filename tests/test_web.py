"""Localhost view: loopback bind, Host allowlist, CSP, read-only access and escaping."""

import contextlib
import hashlib
import http.client
import io
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jevtrader import brief, web
from jevtrader.common import digest, load_strategy
from jevtrader.store import Ledger

NOW = "2026-03-10T13:45:00Z"
PORT = 8765
HOST = f"127.0.0.1:{PORT}"
EVENT_ID = "sec:0000000001-26-000001:ex99.htm"
HOSTILE = (
    "<script>alert(1)</script> The company raised guidance for the full year. "
    "<img src=x onerror=alert(2)> Strong demand continued across every region we serve."
)


def populate(path: Path) -> None:
    with Ledger(path) as ledger:
        ledger.disclosure(
            {
                "id": EVENT_ID,
                "symbol": "ABC",
                "text": HOSTILE,
                "source_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm",
                "mode": "forward",
                "published_at": "2026-03-10T01:00:00Z",
                "first_seen_at": "2026-03-10T01:00:00Z",
                "form": "8-K",
                "items": ["7.01"],
            },
            imported=False,
        )
        ledger.disclosure(
            {
                "id": "later",
                "symbol": "XYZ",
                "text": "Record revenue was reported for the third consecutive quarter.",
                "source_url": "https://www.sec.gov/Archives/edgar/data/2/later.htm",
                "mode": "forward",
                "published_at": "2026-03-10T14:00:00Z",
                "first_seen_at": "2026-03-10T14:00:00Z",
            },
            imported=False,
        )
        ledger.put(
            "forecasts",
            "f1",
            {
                "id": "f1",
                "event_id": EVENT_ID,
                "symbol": "ABC",
                "decision_at": "2026-03-10T01:05:00Z",
                "recorded_at": "2026-03-10T01:05:00Z",
                "mode": "forward",
                "eligibility": "forward",
                "action": "WATCH",
                "reasons": ["<b>no trained calibrator</b>"],
                "expected_return": None,
                "strategy": load_strategy(),
                "provider": "rules",
                "resolved_model": "rules-v1",
            },
        )
        ledger.put(
            "runs",
            "run-1",
            {
                "job": "poll",
                "started_at": "2026-03-10T13:40:00Z",
                "finished_at": "2026-03-10T13:40:02Z",
                "status": "error",
                "error": "<i>boom</i>",
                "counts": {"new": 1},
            },
        )


class WebTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.path = self.dir / "forward.sqlite"
        populate(self.path)

    def get(self, target, *, host=HOST, method="GET", watchlist=("ABC",), path=None):
        hosts = [host] if isinstance(host, str) else list(host)
        return web.respond(
            path or self.path, method, target, hosts, port=PORT, now=NOW, watchlist=watchlist
        )

    def text(self, target, **options):
        status, headers, body = self.get(target, **options)
        return status, headers, body.decode()


class GuardTests(WebTestCase):
    def test_host_header_must_name_loopback_and_this_port(self):
        for host in (HOST, f"localhost:{PORT}", f"LOCALHOST:{PORT}"):
            with self.subTest(host=host):
                self.assertEqual(self.get("/", host=host)[0], 200)
        rejected = (
            "evil.example",
            f"evil.example:{PORT}",
            f"127.0.0.1:{PORT + 1}",
            "127.0.0.1",
            "localhost",
            "",
            f"127.0.0.1:{PORT}.evil.example",
            f"[::1]:{PORT}",
            f"0.0.0.0:{PORT}",
            f"127.0.0.2:{PORT}",
            f"user@127.0.0.1:{PORT}",
            [],
            [HOST, HOST],
            [HOST, "evil.example"],
        )
        with patch.object(web, "open_readonly", side_effect=AssertionError("ledger opened")):
            for host in rejected:
                with self.subTest(host=host):
                    status, headers, _ = self.get("/", host=host)
                    self.assertEqual(status, 403)
                    self.assertEqual(headers["Content-Security-Policy"], web.CSP)

    def test_only_get_and_head_are_served(self):
        with patch.object(web, "open_readonly", side_effect=AssertionError("ledger opened")):
            for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
                with self.subTest(method=method):
                    status, headers, _ = self.get("/", method=method)
                    self.assertEqual(status, 405)
                    self.assertEqual(headers["Allow"], "GET, HEAD")

    def test_request_targets_must_be_origin_form_paths(self):
        for target in ("http://evil.example/", "//evil.example/", "*", "", "/" + "a" * 3000):
            with self.subTest(target=target[:30]):
                self.assertEqual(self.get(target)[0], 400)
        self.assertEqual(self.get("/filing/%ff%fe")[0], 400)

    def test_every_response_carries_the_security_headers(self):
        for target in (
            "/",
            "/scoreboard",
            "/health",
            "/api/brief.json",
            "/static/app.css",
            "/nope",
        ):
            with self.subTest(target=target):
                _, headers, _ = self.get(target)
                self.assertEqual(
                    headers["Content-Security-Policy"],
                    "default-src 'none'; style-src 'self'; img-src 'self' data:; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                )
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_bind_address_must_be_loopback(self):
        for host in ("0.0.0.0", "::", "::1", "192.168.1.10", "example.com", ""):
            with self.subTest(host=host), self.assertRaises(ValueError):
                web.make_server(self.path, host=host, port=0)
        for port in (-1, 65_536, "8765", True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                web.make_server(self.path, port=port)
        with self.assertRaises(ValueError):
            web.make_server(self.path, port=0, watchlist=["not a symbol"])


class PageTests(WebTestCase):
    def assert_page(self, body):
        self.assertIn("Research tool, not investment advice.", body)
        self.assertIn("Collecting evidence: 0 of 100 matured calls", body)
        self.assertIn('<link rel="stylesheet" href="/static/app.css">', body)
        self.assertNotIn("<script", body)
        self.assertNotIn("style=", body)
        self.assertNotIn("<form", body)

    def test_brief_page_escapes_filing_text_and_hides_future_filings(self):
        status, headers, body = self.text("/?q=<script>reflected</script>")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assert_page(body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("reflected", body)
        self.assertNotIn("<b>", body)
        self.assertNotIn("XYZ", body)  # First seen after now.
        self.assertIn('href="/filing/sec%3A0000000001-26-000001%3Aex99.htm"', body)

    def test_filing_page_and_not_found_cases(self):
        status, _, body = self.text("/filing/sec%3A0000000001-26-000001%3Aex99.htm")
        self.assertEqual(status, 200)
        self.assert_page(body)
        self.assertIn("&lt;b&gt;no trained calibrator&lt;/b&gt;", body)
        self.assertEqual(self.text(f"/filing/{EVENT_ID}")[0], 200)
        for target in (
            "/filing/later",
            "/filing/missing",
            "/filing/",
            "/filing/a%2Fb",
            "/filing/<x>",
        ):
            with self.subTest(target=target):
                status, _, body = self.text(target)
                self.assertEqual(status, 404)
                self.assert_page(body)
                self.assertNotIn("Record revenue", body)

    def test_scoreboard_and_health_pages(self):
        status, _, body = self.text("/scoreboard")
        self.assertEqual(status, 200)
        self.assert_page(body)
        self.assertIn(brief.percent(web.evidence.GATE["futility_upper_bps"] / 10_000), body)
        self.assertIn(digest(web.evidence.GATE), body)
        status, _, body = self.text("/health")
        self.assertEqual(status, 200)
        self.assert_page(body)
        self.assertIn("Needs attention: poll.", body)
        self.assertIn("&lt;i&gt;boom&lt;/i&gt;", body)

    def test_unknown_route_is_a_calm_404(self):
        status, _, body = self.text("/admin")
        self.assertEqual(status, 404)
        self.assert_page(body)

    def test_json_endpoints(self):
        status, headers, body = self.get("/api/brief.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertNotIn(b"<", body)
        data = json.loads(body)
        self.assertEqual([f["event_id"] for f in data["filings"]], [EVENT_ID])
        self.assertTrue(data["filings"][0]["watchlist"])
        self.assertNotIn(HOSTILE, body.decode())
        status, _, body = self.get("/api/scoreboard.json")
        board = json.loads(body)
        self.assertEqual((status, board["status"]), (200, "collecting"))
        self.assertEqual(board["gate_sha256"], digest(board["gate"]))

    def test_stylesheet_is_calm_and_adaptive(self):
        status, headers, body = self.text("/static/app.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertIn("prefers-color-scheme: dark", body)
        self.assertIn("max-width: 480px", body)
        self.assertNotIn("url(", body)  # Nothing external for the CSP to block.

    def test_missing_ledger_is_503_and_nothing_is_created(self):
        missing = self.dir / "absent" / "forward.sqlite"
        status, _, body = self.text("/", path=missing)
        self.assertEqual(status, 503)
        self.assertIn("Research tool, not investment advice.", body)
        self.assertFalse(missing.parent.exists())


class ReadOnlyTests(WebTestCase):
    def test_serving_never_writes_the_ledger(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        opened = []
        real = web.open_readonly

        def spy(path):
            ledger = real(path)
            opened.append(ledger)
            return ledger

        with patch.object(web, "open_readonly", side_effect=spy):
            for target in ("/", "/scoreboard", "/health", f"/filing/{EVENT_ID}", "/api/brief.json"):
                self.assertEqual(self.get(target)[0], 200)
        self.assertEqual(len(opened), 5)
        self.assertTrue(all(getattr(ledger, "readonly", True) for ledger in opened))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        with Ledger(self.path) as ledger:
            self.assertEqual(ledger.counts()["runs"], 1)

    def test_fallback_reader_when_store_lacks_readonly(self):
        class OldLedger:
            def __init__(self, path):
                raise AssertionError("a writable ledger must never be opened")

        with patch.object(web, "Ledger", OldLedger):
            ledger = web.open_readonly(self.path)
            self.assertIsInstance(ledger, web.ReadOnlyLedger)
            with ledger:
                self.assertEqual(ledger.get("forecasts", "f1")["action"], "WATCH")
                self.assertIsNone(ledger.get("forecasts", "missing"))
                self.assertEqual([r["id"] for r in ledger.all("disclosures")], ["later", EVENT_ID])
                self.assertEqual(len(ledger.prefix("disclosures", "sec:")), 1)
                with self.assertRaises(sqlite3.OperationalError):
                    ledger.db.execute("INSERT INTO records VALUES ('runs', 'x', '{}', 'h', 't')")
            self.assertEqual(self.get("/")[0], 200)
        with self.assertRaises(ValueError):
            web.ReadOnlyLedger(self.dir / "absent.sqlite")

    def test_fallback_reader_detects_tampered_records(self):
        path = self.dir / "tampered.sqlite"
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE records (kind TEXT, id TEXT, payload TEXT, content_hash TEXT, recorded_at TEXT)"
        )
        db.execute(
            "INSERT INTO records VALUES ('runs', 'r', ?, ?, 't')",
            (json.dumps({"job": "poll"}), digest({"job": "other"})),
        )
        db.commit()
        db.close()
        with web.ReadOnlyLedger(path) as ledger, self.assertRaises(ValueError):
            ledger.all("runs")


class SocketTests(WebTestCase):
    def setUp(self):
        super().setUp()
        self.server = web.make_server(self.path, port=0, clock=lambda: NOW, quiet=True)
        self.port = self.server.server_address[1]
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.server.RequestHandlerClass.timeout, web.REQUEST_TIMEOUT_SECONDS)
        # A short poll interval keeps shutdown() in cleanup from waiting half a second per test.
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, target, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        conn.endheaders()
        response = conn.getresponse()
        return response, response.read()

    def test_serves_pages_over_loopback(self):
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"Research tool, not investment advice.", body)
        self.assertEqual(response.getheader("Content-Security-Policy"), web.CSP)
        self.assertEqual(int(response.getheader("Content-Length")), len(body))
        self.assertNotIn("Python", response.getheader("Server"))
        response, body = self.request("HEAD", "/scoreboard")
        self.assertEqual((response.status, body), (200, b""))
        response, _ = self.request("GET", "/", host=f"localhost:{self.port}")
        self.assertEqual(response.status, 200)

    def test_rebinding_hosts_and_writes_are_refused(self):
        response, _ = self.request("GET", "/", host=f"attacker.example:{self.port}")
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/")
        self.assertEqual(response.status, 405)
        response, _ = self.request("BREW", "/")
        self.assertEqual(response.status, 501)
        self.assertEqual(response.getheader("Content-Security-Policy"), web.CSP)

    def test_request_log_escapes_terminal_control_characters(self):
        self.server.quiet = False
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
                raw.sendall(
                    b"GET /\x1b]0;pwned\x07\x1b[2J\x9b31m HTTP/1.1\r\nHost: evil.example\r\n\r\n"
                )
                reply = raw.recv(65_536).decode("latin-1")
        self.assertTrue(reply.startswith("HTTP/1.0 403"), reply[:40])
        line = log.getvalue()
        self.assertIn(r'"GET /\x1b]0;pwned\x07\x1b[2J\x9b31m HTTP/1.1" 403', line)
        self.assertTrue(line.endswith("\n") and line[:-1].isprintable(), repr(line))

    def test_malformed_requests_get_the_same_headers(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            raw.sendall(b"GET / extra HTTP/1.1\r\n\r\n")
            reply = raw.recv(65_536).decode("latin-1")
        self.assertTrue(reply.startswith("HTTP/1.0 400"), reply[:40])
        self.assertIn("Content-Security-Policy: default-src 'none'", reply)
        self.assertNotIn("<html", reply.lower())


if __name__ == "__main__":
    unittest.main()
