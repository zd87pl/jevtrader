"""Alpaca market data through an injected transport; these tests never reach the network."""

import base64
import io
import json
import math
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from jevtrader import bars, paths
from jevtrader.common import instant
from jevtrader.market import normalize_bar
from jevtrader.store import Ledger

ET = ZoneInfo("America/New_York")
KEY_VALUE = "PKTESTKEYID0000000001"
SECRET_VALUE = "TestSecretValue00000000000000000000002"
KEYS = {"ALPACA_API_KEY_ID": KEY_VALUE, "ALPACA_API_SECRET_KEY": SECRET_VALUE}
DEFAULT_URLOPEN = bars.urlopen  # Captured before setUp replaces it with a guard.
HOLIDAYS = frozenset({date(2026, 2, 16)})  # Fixture calendar only.
NOW = "2026-03-13T20:20:00Z"  # 16:20 EDT on a Friday, 20 minutes after the close.
RECEIPT = "2026-03-13T20:30:00.000000Z"


def weekdays(start, end, holidays=HOLIDAYS):
    day, result = start, []
    while day <= end:
        if day.weekday() < 5 and day not in holidays:
            result.append(day)
        day += timedelta(days=1)
    return result


def stamp(day, style="et"):
    if style == "utc":
        return f"{day.isoformat()}T00:00:00Z"
    moment = datetime(day.year, day.month, day.day, tzinfo=ET).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def raw_bar(day, price=100.0, **changes):
    row = {
        "t": stamp(day),
        "o": price,
        "h": price + 2,
        "l": price - 2,
        "c": price + 1,
        "v": 1_000_000,
        "n": 900,
        "vw": price,
    }
    row.update(changes)
    return row


def series(start=date(2026, 1, 2), end=date(2026, 3, 13), price=100.0):
    return [raw_bar(day, price + index) for index, day in enumerate(weekdays(start, end))]


def split(name, ex_date, new, old, **changes):
    item = {
        "id": f"split-{name}-{ex_date}-{new}-{old}",
        "symbol": name,
        "new_rate": new,
        "old_rate": old,
        "ex_date": ex_date,
        "process_date": ex_date,
        "record_date": ex_date,
        "payable_date": ex_date,
    }
    item.update(changes)
    return item


def dividend(name, ex_date, rate, **changes):
    item = {
        "id": f"dividend-{name}-{ex_date}-{rate}",
        "symbol": name,
        "rate": rate,
        "special": False,
        "foreign": False,
        "ex_date": ex_date,
        "process_date": ex_date,
        "record_date": ex_date,
        "payable_date": ex_date,
    }
    item.update(changes)
    return item


def page_token(offset):
    return base64.b64encode(f"offset:{offset}".encode()).decode()


def http_error(status, body=b"remote body with leaked-detail"):
    return HTTPError("https://example.invalid", status, "error", None, io.BytesIO(body))


def returning(payload):
    """A transport that answers every request with the given JSON value or raw bytes."""
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        return payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    transport.calls = calls
    return transport


class FakeAlpaca:
    """Answers like Alpaca's calendar, bars and corporate-actions endpoints; records requests."""

    def __init__(
        self,
        bars=None,
        actions=None,
        *,
        holidays=HOLIDAYS,
        early=None,
        page_size=None,
        fail=None,
        ignore_window=False,
    ):
        self.bars = bars or {}
        self.actions = actions or {}
        self.holidays = holidays
        self.early = early or {}
        self.page_size = page_size
        self.fail = fail or {}  # path -> query -> exception or None
        self.ignore_window = ignore_window
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        failure = self.fail.get(parts.path)
        error = failure(query) if failure else None
        if error is not None:
            raise error
        if parts.path == "/v2/calendar":
            return json.dumps(self._calendar(query)).encode()
        if parts.path == "/v2/stocks/bars":
            return self._bars(query)
        if parts.path == "/v1/corporate-actions":
            return self._actions(query)
        raise AssertionError(f"unexpected path {parts.path}")

    def queries(self, path):
        return [
            dict(parse_qsl(urlsplit(call["url"]).query))
            for call in self.calls
            if urlsplit(call["url"]).path == path
        ]

    def _calendar(self, query):
        start, end = date.fromisoformat(query["start"]), date.fromisoformat(query["end"])
        return [
            {
                "date": day.isoformat(),
                "open": "09:30",
                "close": self.early.get(day, "16:00"),
                "session_open": "0400",
                "session_close": "2000",
                "settlement_date": (day + timedelta(days=1)).isoformat(),
            }
            for day in weekdays(start, end, self.holidays)
        ]

    def _page(self, items, query):
        offset = 0
        if "page_token" in query:
            offset = int(base64.b64decode(query["page_token"]).decode().split(":")[1])
        size = self.page_size or max(len(items), 1)
        more = offset + size < len(items)
        return items[offset : offset + size], page_token(offset + size) if more else None

    def _bars(self, query):
        start, end = instant(query["start"]), instant(query["end"])
        items = [
            (name, row)
            for name in query["symbols"].split(",")
            for row in self.bars.get(name, [])
            if self.ignore_window or start <= instant(row["t"]) <= end
        ]
        page, token = self._page(items, query)
        grouped = {}
        for name, row in page:
            grouped.setdefault(name, []).append(row)
        return json.dumps({"bars": grouped, "next_page_token": token, "currency": "USD"}).encode()

    def _actions(self, query):
        wanted = set(query["symbols"].split(","))
        items = [
            (group, item)
            for group, rows in self.actions.items()
            for item in rows
            if item.get("symbol") in wanted
            and (self.ignore_window or query["start"] <= item["ex_date"] <= query["end"])
        ]
        page, token = self._page(items, query)
        grouped = {}
        for group, item in page:
            grouped.setdefault(group, []).append(item)
        return json.dumps({"corporate_actions": grouped, "next_page_token": token}).encode()


def forward_bar(name, day, price):
    row = raw_bar(day, price)
    session = {
        "symbol": name,
        "session": day.isoformat(),
        "open_at": f"{day.isoformat()}T13:30:00Z",
        "close_at": f"{day.isoformat()}T20:00:00Z",
        "open": row["o"],
        "high": row["h"],
        "low": row["l"],
        "close": row["c"],
        "volume": row["v"],
    }
    with patch("jevtrader.market.utc_now", return_value=RECEIPT):
        return normalize_bar(session, mode="forward")


class AlpacaTestCase(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, KEYS)
        environment.start()
        self.addCleanup(environment.stop)
        # A missing transport must never reach the network from a test.
        guard = patch.object(bars, "urlopen", side_effect=AssertionError("real network call"))
        guard.start()
        self.addCleanup(guard.stop)
        self.sleeps = []
        limiter = patch.object(
            bars, "_LIMITER", bars._RateLimiter(time.monotonic, self.sleeps.append)
        )
        limiter.start()
        self.addCleanup(limiter.stop)
        self.clock(RECEIPT)
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)

    def clock(self, value):
        for target in ("jevtrader.bars.utc_now", "jevtrader.market.utc_now"):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def market(self, **kwargs):
        kwargs.setdefault("bars", {"ABC": series(), "SPY": series(price=500.0)})
        return FakeAlpaca(**kwargs)

    def stored(self, name):
        return {bar["session"]: bar for bar in self.ledger.prefix("bars", f"{name}:")}

    def assertNoSecrets(self, value):
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        self.assertNotIn(KEY_VALUE, text)
        self.assertNotIn(SECRET_VALUE, text)


class UrlAllowlistTests(unittest.TestCase):
    BARS = {
        "symbols": "AAPL,BRK.B",
        "timeframe": "1Day",
        "start": "2026-03-09T16:00:00Z",
        "end": "2026-03-13T16:00:00Z",
        "limit": "10000",
        "adjustment": "raw",
        "feed": "sip",
        "sort": "asc",
    }
    ACTIONS = {
        "symbols": "AAPL",
        "types": bars.ACTION_TYPES,
        "start": "2026-03-02",
        "end": "2026-03-13",
        "limit": "1000",
        "sort": "asc",
    }

    def url(self, params, host="data.alpaca.markets", path="/v2/stocks/bars"):
        return f"https://{host}{path}?{urlencode(params)}"

    def test_approved_routes(self):
        bars.validate_url(self.url(self.BARS))
        bars.validate_url(self.url({**self.BARS, "page_token": "QUFQTHxEfDIw+/Mi0wMQ=="}))
        bars.validate_url(self.url(self.ACTIONS, path="/v1/corporate-actions"))
        bars.validate_url(
            self.url(
                {"start": "2026-01-01", "end": "2026-03-13"},
                host="paper-api.alpaca.markets",
                path="/v2/calendar",
            )
        )

    def test_rejects_other_origins_and_paths(self):
        query = urlencode(self.BARS)
        for url in (
            f"http://data.alpaca.markets/v2/stocks/bars?{query}",
            f"https://user:pw@data.alpaca.markets/v2/stocks/bars?{query}",
            f"https://data.alpaca.markets:443/v2/stocks/bars?{query}",
            f"https://api.alpaca.markets/v2/stocks/bars?{query}",
            f"https://data.alpaca.markets.evil.test/v2/stocks/bars?{query}",
            f"https://DATA.alpaca.markets/v2/stocks/bars?{query}",
            f"https://data.alpaca.markets/v2/stocks/AAPL/bars?{query}",
            f"https://data.alpaca.markets/v2/stocks/bars/?{query}",
            f"https://data.alpaca.markets/v2/stocks/bars?{query}#x",
            f"https://paper-api.alpaca.markets/v2/stocks/bars?{query}",
            f"https://data.alpaca.markets/v2/calendar?{query}",
            "https://data.alpaca.markets:bad/v2/stocks/bars",
        ):
            with self.subTest(url=url), self.assertRaises(bars.BarsError):
                bars.validate_url(url)

    def test_rejects_unapproved_parameters(self):
        cases = [
            {**self.BARS, "asof": "2026-01-01"},
            {key: value for key, value in self.BARS.items() if key != "adjustment"},
            {**self.BARS, "timeframe": "1Min"},
            {**self.BARS, "adjustment": "all"},
            {**self.BARS, "feed": "iex"},
            {**self.BARS, "sort": "desc"},
            {**self.BARS, "symbols": "aapl"},
            {**self.BARS, "symbols": "AAPL,,MSFT"},
            {**self.BARS, "symbols": ",".join(f"S{index:03d}" for index in range(101))},
            {**self.BARS, "start": "2026-03-09"},
            {**self.BARS, "limit": "0"},
            {**self.BARS, "page_token": "has space"},
            {**self.BARS, "page_token": ""},
        ]
        for params in cases:
            with self.subTest(params=params), self.assertRaises(bars.BarsError):
                bars.validate_url(self.url(params))
        with self.assertRaises(bars.BarsError):
            bars.validate_url(self.url(self.BARS) + "&feed=sip")  # Duplicate parameter.
        with self.assertRaises(bars.BarsError):
            actions = {**self.ACTIONS, "types": "forward_split,spin_off"}
            bars.validate_url(self.url(actions, path="/v1/corporate-actions"))
        with self.assertRaises(bars.BarsError):
            bars.validate_url(
                self.url(
                    {"start": "2026-01-01T00:00:00Z", "end": "2026-03-13"},
                    host="paper-api.alpaca.markets",
                    path="/v2/calendar",
                )
            )

    def test_one_hundred_symbols_fit_one_request(self):
        symbols = ",".join(f"S{index:03d}" for index in range(100))
        bars.validate_url(self.url({**self.BARS, "symbols": symbols}))


class TransportTests(AlpacaTestCase):
    def test_keys_travel_only_in_headers_to_approved_hosts(self):
        fake = self.market()
        bars.fetch_forward(self.ledger, ["ABC"], now=NOW, lookback_sessions=2, transport=fake)
        self.assertEqual(len(fake.calls), 3)
        for call in fake.calls:
            headers = call["headers"]
            self.assertEqual(headers["APCA-API-KEY-ID"], KEY_VALUE)
            self.assertEqual(headers["APCA-API-SECRET-KEY"], SECRET_VALUE)
            self.assertEqual(headers["Accept"], "application/json")
            self.assertTrue(headers["User-Agent"].startswith(f"{paths.APP_NAME}/"))
            self.assertEqual(call["timeout"], bars.REQUEST_TIMEOUT)
            self.assertNoSecrets(call["url"])
            bars.validate_url(call["url"])
        hosts = [urlsplit(call["url"]).netloc for call in fake.calls]
        self.assertEqual(
            hosts, ["paper-api.alpaca.markets", "data.alpaca.markets", "data.alpaca.markets"]
        )

    def test_missing_keys_fail_before_any_request(self):
        for missing in (list(KEYS), ["ALPACA_API_SECRET_KEY"], ["ALPACA_API_KEY_ID"]):
            fake = self.market()
            with self.subTest(missing=missing), patch.dict(os.environ, {n: " " for n in missing}):
                calls = (
                    lambda: bars.calendar(date(2026, 3, 2), date(2026, 3, 13), transport=fake),
                    lambda: bars.daily_bars(
                        ["ABC"], date(2026, 3, 2), date(2026, 3, 13), transport=fake
                    ),
                    lambda: bars.corporate_actions(
                        ["ABC"], date(2026, 3, 2), date(2026, 3, 13), transport=fake
                    ),
                    lambda: bars.fetch_forward(self.ledger, ["ABC"], now=NOW, transport=fake),
                    lambda: bars.fetch_historical(
                        self.ledger, ["ABC"], date(2026, 3, 2), date(2026, 3, 13), transport=fake
                    ),
                )
                for call in calls:
                    with self.assertRaises(bars.MissingKeys) as caught:
                        call()
                    self.assertIn("ALPACA_API_KEY_ID", str(caught.exception))
                    self.assertIsInstance(caught.exception, ValueError)
                self.assertEqual(fake.calls, [])

    def test_unsafe_key_characters_are_refused_without_echo(self):
        fake = self.market()
        for value in ("PK\r\nX-Injected: 1", "PK KEY", "PK\tKEY", "PK%0D"):
            with self.subTest(value=value), patch.dict(os.environ, {"ALPACA_API_KEY_ID": value}):
                with self.assertRaises(bars.BarsError) as caught:
                    bars.calendar(date(2026, 3, 2), date(2026, 3, 13), transport=fake)
                self.assertIn("ALPACA_API_KEY_ID", str(caught.exception))
                self.assertNotIn(value, str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_http_errors_report_status_only(self):
        for status in (401, 403, 429, 500):
            fake = self.market(fail={"/v2/calendar": lambda query, s=status: http_error(s)})
            with self.subTest(status=status), self.assertRaises(bars.BarsError) as caught:
                bars.calendar(date(2026, 3, 2), date(2026, 3, 13), transport=fake)
            self.assertEqual(caught.exception.status, status)
            self.assertIn(str(status), str(caught.exception))
            self.assertNotIn("leaked-detail", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_auth_failures_point_at_keys_and_subscription(self):
        fake = self.market(fail={"/v2/calendar": lambda query: http_error(403)})
        with self.assertRaises(bars.BarsError) as caught:
            bars.calendar(date(2026, 3, 2), date(2026, 3, 13), transport=fake)
        self.assertIn("API keys", str(caught.exception))
        self.assertIn("subscription", str(caught.exception))

    def test_transport_exception_text_never_leaks(self):
        def leaky(url, headers, timeout):
            raise RuntimeError(f"connection reset while sending {headers}")

        with self.assertRaises(bars.BarsError) as caught:
            bars.calendar(date(2026, 3, 2), date(2026, 3, 13), transport=leaky)
        self.assertNoSecrets(str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.status)

    def test_malformed_payloads_fail_closed(self):
        for payload in (b"{not json", b"\xff\xfe\x00", b"[" * 100_000):
            with self.subTest(payload=payload[:10]), self.assertRaises(bars.BarsError):
                bars.calendar(date(2026, 3, 2), date(2026, 3, 13), transport=returning(payload))

        def text(url, headers, timeout):
            return "[]"

        with self.assertRaisesRegex(bars.BarsError, "not bytes"):
            bars.calendar(date(2026, 3, 2), date(2026, 3, 13), transport=text)

    def test_oversized_response_is_refused(self):
        with patch.object(bars, "MAX_RESPONSE_BYTES", 10):
            with self.assertRaisesRegex(bars.BarsError, "size limit"):
                bars.calendar(
                    date(2026, 3, 2),
                    date(2026, 3, 13),
                    transport=returning(b"[" + b" " * 20 + b"]"),
                )

    def test_request_budget_bounds_pagination(self):
        fake = self.market(page_size=1)
        with patch.object(bars, "MAX_REQUESTS", 3):
            with self.assertRaisesRegex(bars.BarsError, "budget"):
                bars.daily_bars(["ABC"], date(2026, 3, 2), date(2026, 3, 13), transport=fake)
        self.assertEqual(len(fake.calls), 3)

    def test_every_request_is_paced(self):
        limiter = Mock()
        fake = self.market()
        with patch.object(bars, "_LIMITER", limiter):
            bars.fetch_forward(self.ledger, ["ABC"], now=NOW, lookback_sessions=2, transport=fake)
        self.assertEqual(limiter.acquire.call_count, len(fake.calls))

    def test_rate_limiter_spaces_requests(self):
        now = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        limiter = bars._RateLimiter(lambda: now[0], sleep)
        limiter.acquire()
        now[0] += 0.1
        limiter.acquire()
        now[0] += 5
        limiter.acquire()
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], bars.REQUEST_INTERVAL - 0.1)
        self.assertLessEqual(bars.REQUEST_INTERVAL * 200, 60.0 + 1e-9)


class DefaultTransportTests(AlpacaTestCase):
    class Response:
        def __init__(self, url, body):
            self.url, self.body, self.reads = url, body, []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def geturl(self):
            return self.url

        def read(self, limit):
            self.reads.append(limit)
            return self.body

    def opener(self, response):
        opener = Mock()
        opener.open.return_value = response
        return opener

    def test_urllib_transport_sends_headers_without_redirects(self):
        url = "https://paper-api.alpaca.markets/v2/calendar?start=2026-03-02&end=2026-03-02"
        response = self.Response(url, b"[]")
        opener = self.opener(response)
        with patch.object(bars, "build_opener", return_value=opener) as build:
            body = DEFAULT_URLOPEN(url, {"APCA-API-KEY-ID": KEY_VALUE}, 7.0)
        self.assertEqual(body, b"[]")
        self.assertIsInstance(build.call_args.args[0], bars._NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.get_header("Apca-api-key-id"), KEY_VALUE)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 7.0})
        self.assertEqual(response.reads, [bars.MAX_RESPONSE_BYTES + 1])
        self.assertIsNone(bars._NoRedirect().redirect_request(None, None, 302, "", {}, "x"))

    def test_urllib_transport_refuses_a_moved_response(self):
        url = "https://paper-api.alpaca.markets/v2/calendar?start=2026-03-02&end=2026-03-02"
        opener = self.opener(self.Response("https://elsewhere.test/", b"[]"))
        with patch.object(bars, "build_opener", return_value=opener):
            with self.assertRaisesRegex(bars.BarsError, "redirect"):
                DEFAULT_URLOPEN(url, {}, 1.0)

    def test_client_uses_urllib_transport_by_default(self):
        opener = Mock()
        opener.open.side_effect = lambda request, timeout: self.Response(
            request.full_url,
            json.dumps([{"date": "2026-03-02", "open": "09:30", "close": "16:00"}]).encode(),
        )
        with (
            patch.object(bars, "urlopen", DEFAULT_URLOPEN),
            patch.object(bars, "build_opener", return_value=opener),
        ):
            sessions = bars.calendar(date(2026, 3, 2), date(2026, 3, 2))
        self.assertEqual(sessions[0]["session"], "2026-03-02")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Apca-api-secret-key"), SECRET_VALUE)


class CalendarTests(AlpacaTestCase):
    def test_sessions_convert_new_york_times_across_daylight_saving(self):
        fake = self.market()
        sessions = bars.calendar(date(2026, 3, 6), date(2026, 3, 9), transport=fake)
        self.assertEqual(
            sessions,
            [
                {
                    "session": "2026-03-06",
                    "open_at": "2026-03-06T14:30:00.000000Z",
                    "close_at": "2026-03-06T21:00:00.000000Z",
                },
                {
                    "session": "2026-03-09",
                    "open_at": "2026-03-09T13:30:00.000000Z",
                    "close_at": "2026-03-09T20:00:00.000000Z",
                },
            ],
        )
        self.assertEqual(
            fake.calls[0]["url"],
            "https://paper-api.alpaca.markets/v2/calendar?start=2026-03-06&end=2026-03-09",
        )

    def test_early_close_and_holidays(self):
        fake = self.market(holidays={date(2026, 11, 26)}, early={date(2026, 11, 27): "13:00"})
        sessions = bars.calendar(date(2026, 11, 25), date(2026, 11, 30), transport=fake)
        self.assertEqual(
            [s["session"] for s in sessions], ["2026-11-25", "2026-11-27", "2026-11-30"]
        )
        self.assertEqual(sessions[1]["close_at"], "2026-11-27T18:00:00.000000Z")

    def test_rows_are_sorted(self):
        rows = [
            {"date": "2026-03-03", "open": "09:30", "close": "16:00"},
            {"date": "2026-03-02", "open": "09:30", "close": "16:00"},
        ]
        sessions = bars.calendar(date(2026, 3, 2), date(2026, 3, 3), transport=returning(rows))
        self.assertEqual([s["session"] for s in sessions], ["2026-03-02", "2026-03-03"])

    def test_malformed_calendars_fail_closed(self):
        good = {"date": "2026-03-02", "open": "09:30", "close": "16:00"}
        cases = {
            "object": {"calendar": [good]},
            "row type": [["2026-03-02", "09:30", "16:00"]],
            "outside": [{**good, "date": "2026-03-04"}],
            "repeated": [good, good],
            "too many": [good] * 3,
            "date format": [{**good, "date": "20260302"}],
            "impossible date": [{**good, "date": "2026-02-30"}],
            "short time": [{**good, "open": "9:30"}],
            "seconds": [{**good, "close": "16:00:00"}],
            "missing time": [{"date": "2026-03-02", "open": "09:30"}],
            "inverted": [{**good, "open": "16:00", "close": "09:30"}],
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(bars.BarsError):
                bars.calendar(date(2026, 3, 2), date(2026, 3, 3), transport=returning(payload))

    def test_date_inputs_are_checked_before_requests(self):
        fake = self.market()
        for start, end in (
            (datetime(2026, 3, 2, tzinfo=timezone.utc), date(2026, 3, 3)),
            ("2026-03-02", date(2026, 3, 3)),
            (date(2026, 3, 4), date(2026, 3, 3)),
            (date(1990, 1, 1), date(2026, 3, 3)),
        ):
            with self.subTest(start=start), self.assertRaises(bars.BarsError):
                bars.calendar(start, end, transport=fake)
        self.assertEqual(fake.calls, [])


class DailyBarsTests(AlpacaTestCase):
    def test_bars_by_symbol_with_raw_adjustment(self):
        fake = self.market()
        result = bars.daily_bars(
            ["abc", "SPY", "NONE"], date(2026, 3, 9), date(2026, 3, 13), transport=fake
        )
        self.assertEqual(list(result), ["ABC", "SPY", "NONE"])
        self.assertEqual(result["NONE"], [])
        self.assertEqual(
            [row["session"] for row in result["ABC"]],
            ["2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13"],
        )
        first = result["ABC"][0]
        self.assertEqual(
            set(first), {"symbol", "session", "open", "high", "low", "close", "volume"}
        )
        self.assertEqual(first["symbol"], "ABC")
        self.assertIsInstance(first["volume"], float)
        query = fake.queries("/v2/stocks/bars")[0]
        self.assertEqual(
            query,
            {
                "symbols": "ABC,SPY,NONE",
                "timeframe": "1Day",
                "start": "2026-03-08T16:00:00Z",  # Noon New York time, the day before.
                "end": "2026-03-13T16:00:00Z",
                "limit": "10000",
                "adjustment": "raw",
                "feed": "sip",
                "sort": "asc",
            },
        )

    def test_session_comes_from_the_midnight_stamp(self):
        winter, summer = date(2026, 1, 6), date(2026, 7, 7)
        self.clock("2027-01-01T00:00:00.000000Z")  # P0-15: every requested session has settled.
        stamps = {
            "ET winter": (stamp(winter), winter),
            "ET summer": (stamp(summer), summer),
            "UTC midnight": (stamp(winter, "utc"), winter),
            "offset form": ("2026-01-06T00:00:00-05:00", winter),
        }
        for name, (value, day) in stamps.items():
            payload = {"bars": {"ABC": [raw_bar(day, t=value)]}, "next_page_token": None}
            with self.subTest(name):
                result = bars.daily_bars(
                    ["ABC"], date(2026, 1, 1), date(2026, 12, 31), transport=returning(payload)
                )
                self.assertEqual(result["ABC"][0]["session"], day.isoformat())
        for value in ("2026-01-06T14:30:00Z", "2026-01-06", "2026-01-06T05:00:00", None, 5):
            payload = {"bars": {"ABC": [raw_bar(winter, t=value)]}, "next_page_token": None}
            with self.subTest(value=value), self.assertRaises(bars.BarsError):
                bars.daily_bars(
                    ["ABC"], date(2026, 1, 1), date(2026, 12, 31), transport=returning(payload)
                )

    def test_pagination_follows_tokens(self):
        fake = self.market(page_size=3)
        result = bars.daily_bars(
            ["ABC", "SPY"], date(2026, 3, 9), date(2026, 3, 13), transport=fake
        )
        self.assertEqual(len(result["ABC"]), 5)
        self.assertEqual(len(result["SPY"]), 5)
        queries = fake.queries("/v2/stocks/bars")
        self.assertEqual(len(queries), 4)
        self.assertNotIn("page_token", queries[0])
        self.assertEqual(queries[1]["page_token"], page_token(3))

    def test_repeated_or_invalid_tokens_fail_closed(self):
        page = {"bars": {"ABC": [raw_bar(date(2026, 3, 9))]}, "next_page_token": "same"}
        with self.assertRaisesRegex(bars.BarsError, "page token"):
            bars.daily_bars(["ABC"], date(2026, 3, 9), date(2026, 3, 13), transport=returning(page))
        for token in (5, "has space", "x" * 2000):
            payload = {**page, "next_page_token": token}
            with self.subTest(token=str(token)[:10]), self.assertRaises(bars.BarsError):
                bars.daily_bars(
                    ["ABC"], date(2026, 3, 9), date(2026, 3, 13), transport=returning(payload)
                )
        empty = {**page, "next_page_token": ""}
        result = bars.daily_bars(
            ["ABC"], date(2026, 3, 9), date(2026, 3, 13), transport=returning(empty)
        )
        self.assertEqual(len(result["ABC"]), 1)

    def test_symbols_are_chunked_by_one_hundred(self):
        names = [f"S{index:03d}" for index in range(150)]
        fake = self.market(bars={"S149": series(date(2026, 3, 9))})
        result = bars.daily_bars(names, date(2026, 3, 9), date(2026, 3, 13), transport=fake)
        sizes = [len(q["symbols"].split(",")) for q in fake.queries("/v2/stocks/bars")]
        self.assertEqual(sizes, [100, 50])
        self.assertEqual(len(result["S149"]), 5)

    def test_share_class_dash_maps_to_alpaca_dot(self):
        fake = self.market(bars={"BRK.B": series(date(2026, 3, 9))})
        result = bars.daily_bars(
            ["BRK-B", "BRK.B"], date(2026, 3, 9), date(2026, 3, 13), transport=fake
        )
        self.assertEqual(fake.queries("/v2/stocks/bars")[0]["symbols"], "BRK.B")
        self.assertEqual({row["symbol"] for row in result["BRK-B"]}, {"BRK-B"})
        self.assertEqual({row["symbol"] for row in result["BRK.B"]}, {"BRK.B"})
        self.assertEqual(len(result["BRK-B"]), 5)

    def test_invalid_values_fail_closed(self):
        day = date(2026, 3, 9)
        cases = {
            "zero open": {"o": 0},
            "text high": {"h": "102"},
            "negative volume": {"v": -1},
            "bool close": {"c": True},
            "missing low": {"l": None},
            "huge": {"v": 10**400},
            "high below close": {"h": 100.5},
            "low above open": {"l": 100.5},
        }
        for name, change in cases.items():
            row = raw_bar(day)
            row.update(change)
            payload = {"bars": {"ABC": [row]}, "next_page_token": None}
            with self.subTest(name), self.assertRaises(bars.BarsError):
                bars.daily_bars(["ABC"], day, day, transport=returning(payload))
        nan = b'{"bars": {"ABC": [{"t": "2026-03-09T04:00:00Z", "o": NaN, "h": 1, "l": 1, "c": 1, "v": 1}]}}'
        with self.assertRaises(bars.BarsError):
            bars.daily_bars(["ABC"], day, day, transport=returning(nan))

    def test_malformed_structures_fail_closed(self):
        day = date(2026, 3, 9)
        cases = {
            "unrequested symbol": {"bars": {"XYZ": [raw_bar(day)]}},
            "list of bars": {"bars": [raw_bar(day)]},
            "rows not a list": {"bars": {"ABC": raw_bar(day)}},
            "row not an object": {"bars": {"ABC": [[1, 2, 3]]}},
            "no bars field": {"next_page_token": None},
            "duplicate session": {"bars": {"ABC": [raw_bar(day), raw_bar(day, 101.0)]}},
            "not an object": [raw_bar(day)],
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(bars.BarsError):
                bars.daily_bars(["ABC"], day, day, transport=returning(payload))
        empty = bars.daily_bars(["ABC"], day, day, transport=returning({"bars": None}))
        self.assertEqual(empty, {"ABC": []})

    def test_rows_outside_the_range_are_dropped(self):
        fake = self.market(ignore_window=True)
        result = bars.daily_bars(["ABC"], date(2026, 3, 12), date(2026, 3, 13), transport=fake)
        self.assertEqual([row["session"] for row in result["ABC"]], ["2026-03-12", "2026-03-13"])

    def test_inputs_are_checked_before_requests(self):
        fake = self.market()
        day = date(2026, 3, 9)
        for symbols in ("ABC", b"ABC", None, [None], [5], ["bad symbol"], ["abc!"]):
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                bars.daily_bars(symbols, day, day, transport=fake)
        with self.assertRaisesRegex(bars.BarsError, "sip"):
            bars.daily_bars(["ABC"], day, day, feed="iex", transport=fake)
        with patch.object(bars, "MAX_SYMBOLS", 2), self.assertRaises(bars.BarsError):
            bars.daily_bars(["A", "B", "C"], day, day, transport=fake)
        self.assertEqual(bars.daily_bars([], day, day, transport=fake), {})
        self.assertEqual(fake.calls, [])


class CorporateActionTests(AlpacaTestCase):
    START, END = date(2026, 2, 1), date(2026, 3, 13)

    def test_splits_and_dividends_by_symbol_and_ex_date(self):
        fake = self.market(
            actions={
                "forward_splits": [split("ABC", "2026-02-17", 4, 1)],
                "reverse_splits": [split("SPY", "2026-02-18", 1, 10)],
                "cash_dividends": [
                    dividend("SPY", "2026-03-02", 1.5),
                    dividend("ABC", "2026-03-10", 0.1),
                    dividend("ABC", "2026-03-10", 0.2, special=True),
                ],
            }
        )
        result = bars.corporate_actions(["ABC", "SPY"], self.START, self.END, transport=fake)
        self.assertEqual(result[("ABC", "2026-02-17")], {"split_ratio": 4.0, "cash_dividend": 0.0})
        self.assertEqual(result[("SPY", "2026-02-18")], {"split_ratio": 0.1, "cash_dividend": 0.0})
        self.assertEqual(result[("SPY", "2026-03-02")], {"split_ratio": 1.0, "cash_dividend": 1.5})
        self.assertAlmostEqual(result[("ABC", "2026-03-10")]["cash_dividend"], 0.3)
        self.assertEqual(len(result), 4)
        self.assertEqual(
            fake.queries("/v1/corporate-actions")[0],
            {
                "symbols": "ABC,SPY",
                "types": "forward_split,reverse_split,cash_dividend",
                "start": "2026-02-01",
                "end": "2026-03-13",
                "limit": "1000",
                "sort": "asc",
            },
        )

    def test_same_day_actions_combine(self):
        fake = self.market(
            actions={
                "forward_splits": [
                    split("ABC", "2026-02-17", 2, 1),
                    split("ABC", "2026-02-17", 3, 1),
                ],
                "cash_dividends": [dividend("SPY", "2026-02-18", 0.5)],
                "reverse_splits": [split("SPY", "2026-02-18", 1, 2)],
            }
        )
        result = bars.corporate_actions(["ABC", "SPY"], self.START, self.END, transport=fake)
        self.assertEqual(result[("ABC", "2026-02-17")], {"split_ratio": 6.0, "cash_dividend": 0.0})
        self.assertEqual(
            result[("SPY", "2026-02-18")],
            {"split_ratio": 0.5, "cash_dividend": 0.5, "ambiguous": True},
        )

    def test_repeated_ids_count_once_and_pages_are_followed(self):
        item = dividend("ABC", "2026-03-10", 0.25)
        fake = self.market(actions={"cash_dividends": [item, dict(item)]}, page_size=1)
        result = bars.corporate_actions(["ABC"], self.START, self.END, transport=fake)
        self.assertEqual(result[("ABC", "2026-03-10")]["cash_dividend"], 0.25)
        self.assertEqual(len(fake.queries("/v1/corporate-actions")), 2)

    def test_actions_outside_the_range_or_symbols_are_ignored(self):
        fake = self.market(
            actions={"cash_dividends": [dividend("ABC", "2026-01-10", 0.25)]}, ignore_window=True
        )
        self.assertEqual(bars.corporate_actions(["ABC"], self.START, self.END, transport=fake), {})
        other = {"corporate_actions": {"cash_dividends": [dividend("XYZ", "2026-03-10", 1.0)]}}
        self.assertEqual(
            bars.corporate_actions(["ABC"], self.START, self.END, transport=returning(other)), {}
        )

    def test_share_class_alias(self):
        fake = self.market(actions={"forward_splits": [split("BRK.B", "2026-03-10", 50, 1)]})
        result = bars.corporate_actions(["BRK-B"], self.START, self.END, transport=fake)
        self.assertEqual(
            result, {("BRK-B", "2026-03-10"): {"split_ratio": 50.0, "cash_dividend": 0.0}}
        )

    def test_malformed_actions_fail_closed(self):
        cases = {
            "unknown group": {"corporate_actions": {"stock_dividends": []}},
            "singular group": {"corporate_actions": {"forward_split": []}},
            "missing": {"next_page_token": None},
            "group not a list": {"corporate_actions": {"cash_dividends": {}}},
            "item not an object": {"corporate_actions": {"cash_dividends": ["ABC"]}},
            "no symbol": {"corporate_actions": {"cash_dividends": [{"rate": 1}]}},
            "zero rate": {
                "corporate_actions": {"forward_splits": [split("ABC", "2026-03-10", 0, 1)]}
            },
            "text rate": {
                "corporate_actions": {"reverse_splits": [split("ABC", "2026-03-10", 1, "10")]}
            },
            "negative dividend": {
                "corporate_actions": {"cash_dividends": [dividend("ABC", "2026-03-10", -1)]}
            },
            "bad ex_date": {
                "corporate_actions": {"cash_dividends": [dividend("ABC", "03/10/2026", 1)]}
            },
            "no ex_date": {"corporate_actions": {"cash_dividends": [dividend("ABC", None, 1)]}},
        }
        for name, payload in cases.items():
            with self.subTest(name), self.assertRaises(bars.BarsError):
                bars.corporate_actions(["ABC"], self.START, self.END, transport=returning(payload))
        self.assertEqual(
            bars.corporate_actions(
                ["ABC"], self.START, self.END, transport=returning({"corporate_actions": {}})
            ),
            {},
        )

    def test_session_placement_uses_the_first_session_on_or_after(self):
        days = ["2026-02-13", "2026-02-17", "2026-02-18"]
        found = [
            ("ABC", date(2026, 2, 16), "split", 2.0),  # Holiday -> next session.
            ("ABC", date(2026, 2, 13), "dividend", 0.1),
            ("ABC", date(2026, 2, 12), "dividend", 9.0),  # Before the window: unknown session.
            ("ABC", date(2026, 2, 19), "dividend", 9.0),  # After the window: not yet due.
        ]
        placed = bars._session_actions(found, {"ABC": ["ABC"]}, days)
        self.assertEqual(
            placed,
            {
                ("ABC", "2026-02-17"): {"split_ratio": 2.0, "cash_dividend": 0.0},
                ("ABC", "2026-02-13"): {"split_ratio": 1.0, "cash_dividend": 0.1},
            },
        )


class FetchForwardTests(AlpacaTestCase):
    def fetch(self, fake, symbols=("ABC",), **kwargs):
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("lookback_sessions", 3)
        return bars.fetch_forward(self.ledger, list(symbols), transport=fake, **kwargs)

    def test_stores_completed_sessions_with_actual_receipt(self):
        fake = self.market()
        result = self.fetch(fake)
        self.assertEqual(
            result, {"symbols": 2, "added": 6, "skipped": 0, "missing": 0, "errors": []}
        )
        json.dumps(result)
        stored = self.stored("ABC")
        self.assertEqual(list(stored), ["2026-03-11", "2026-03-12", "2026-03-13"])
        bar = stored["2026-03-13"]
        self.assertEqual(bar["mode"], "forward")
        self.assertEqual(bar["available_at"], RECEIPT)
        self.assertEqual(bar["open_at"], "2026-03-13T13:30:00.000000Z")
        self.assertEqual(bar["close_at"], "2026-03-13T20:00:00.000000Z")
        self.assertEqual((bar["split_ratio"], bar["cash_dividend"]), (1.0, 0.0))
        self.assertEqual(bar["id"], "ABC:2026-03-13")
        self.assertEqual(len(self.stored("SPY")), 3)
        self.assertEqual(
            fake.queries("/v2/calendar"), [{"start": "2026-02-21", "end": "2026-03-13"}]
        )
        bars_query = fake.queries("/v2/stocks/bars")[0]
        self.assertEqual(bars_query["symbols"], "SPY,ABC")
        self.assertEqual(bars_query["start"], "2026-03-10T16:00:00Z")
        self.assertEqual(bars_query["end"], "2026-03-13T16:00:00Z")
        actions_query = fake.queries("/v1/corporate-actions")[0]
        self.assertEqual(
            (actions_query["start"], actions_query["end"]), ("2026-03-04", "2026-03-13")
        )

    def test_session_counts_only_twenty_minutes_after_close(self):
        fake = self.market()
        self.fetch(fake, now="2026-03-13T20:19:59Z")
        self.assertEqual(list(self.stored("ABC")), ["2026-03-10", "2026-03-11", "2026-03-12"])
        self.fetch(self.market(), now="2026-03-13T20:20:00Z")
        self.assertIn("2026-03-13", self.stored("ABC"))

    def test_early_close_session_completes_earlier(self):
        day = date(2026, 11, 27)
        fake = self.market(
            bars={"ABC": [raw_bar(day)], "SPY": [raw_bar(day, 500.0)]},
            holidays={date(2026, 11, 26)},
            early={day: "13:00"},
        )
        self.clock("2026-11-27T18:30:00.000000Z")
        result = self.fetch(fake, now="2026-11-27T18:19:00Z", lookback_sessions=1)
        self.assertEqual(result["added"], 0)
        self.assertNotIn("2026-11-27", self.stored("ABC"))
        result = self.fetch(fake, now="2026-11-27T18:20:00Z", lookback_sessions=1)
        self.assertEqual(result["added"], 2)
        self.assertEqual(
            self.stored("ABC")["2026-11-27"]["close_at"], "2026-11-27T18:00:00.000000Z"
        )

    def test_an_unfinished_session_is_never_stored(self):
        fake = self.market(ignore_window=True)
        result = self.fetch(fake, now="2026-03-13T18:00:00Z")
        self.assertNotIn("2026-03-13", self.stored("ABC"))
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["added"], 6)

    def test_now_after_the_real_clock_is_refused(self):
        fake = self.market()
        with self.assertRaisesRegex(bars.BarsError, "current time"):
            self.fetch(fake, now="2026-03-13T20:30:00.000001Z")
        with self.assertRaises(ValueError):
            self.fetch(fake, now="2026-03-13T20:20:00")  # No timezone.
        self.assertEqual(fake.calls, [])

    def test_refuses_the_research_ledger(self):
        # Forward bars there would shadow the backfill's historical ones for good.
        fake = self.market()
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {paths.HOME_ENV: home}):
            with Ledger(paths.research_ledger_path()) as research:
                with self.assertRaisesRegex(bars.BarsError, "forward ledger"):
                    bars.fetch_forward(research, ["ABC"], now=NOW, transport=fake)
                self.assertEqual(research.prefix("bars", ""), [])
            with Ledger(paths.ledger_path()) as forward:
                result = bars.fetch_forward(
                    forward, ["ABC"], now=NOW, lookback_sessions=1, transport=fake
                )
            self.assertEqual(result["added"], 2)

    def test_benchmark_is_always_included(self):
        fake = self.market(bars={"SPY": series(price=500.0), "QQQ": series(price=400.0)})
        result = self.fetch(fake, symbols=())
        self.assertEqual((result["symbols"], result["added"]), (1, 3))
        self.assertEqual(len(self.stored("SPY")), 3)
        ledger = Ledger(":memory:")
        self.addCleanup(ledger.db.close)
        result = bars.fetch_forward(
            ledger, ["SPY"], now=NOW, lookback_sessions=1, transport=fake, benchmark="qqq"
        )
        self.assertEqual((result["symbols"], result["added"]), (2, 2))

    def test_existing_bars_keep_their_first_receipt(self):
        original = forward_bar("ABC", date(2026, 3, 13), 50.0)
        self.ledger.put("bars", original["id"], original)
        result = self.fetch(self.market())
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["added"], 5)
        self.assertEqual(self.ledger.get("bars", "ABC:2026-03-13"), original)

    def test_a_repeat_run_requests_only_the_calendar(self):
        self.fetch(self.market())
        fake = self.market()
        result = self.fetch(fake)
        self.assertEqual(
            result, {"symbols": 2, "added": 0, "skipped": 6, "missing": 0, "errors": []}
        )
        self.assertEqual([urlsplit(c["url"]).path for c in fake.calls], ["/v2/calendar"])

    def test_only_missing_sessions_are_requested(self):
        for day in (date(2026, 3, 11), date(2026, 3, 12)):
            for name in ("ABC", "SPY"):
                bar = forward_bar(name, day, 100.0)
                self.ledger.put("bars", bar["id"], bar)
        fake = self.market()
        result = self.fetch(fake)
        self.assertEqual((result["added"], result["skipped"]), (2, 4))
        query = fake.queries("/v2/stocks/bars")[0]
        self.assertEqual(
            (query["start"], query["end"]), ("2026-03-12T16:00:00Z", "2026-03-13T16:00:00Z")
        )

    def test_splits_and_dividends_ride_on_their_session(self):
        fake = self.market(
            actions={
                "forward_splits": [split("ABC", "2026-02-16", 2, 1)],  # Fixture holiday.
                "cash_dividends": [dividend("ABC", "2026-02-20", 0.25)],
                "reverse_splits": [split("SPY", "2026-03-02", 1, 5)],
            }
        )
        result = self.fetch(fake, lookback_sessions=25)
        self.assertEqual(result["errors"], [])
        stored = self.stored("ABC")
        self.assertEqual(stored["2026-02-17"]["split_ratio"], 2.0)
        self.assertEqual(stored["2026-02-20"]["cash_dividend"], 0.25)
        self.assertEqual(self.stored("SPY")["2026-03-02"]["split_ratio"], 0.2)
        untouched = [d for d in stored if d not in ("2026-02-17", "2026-02-20")]
        self.assertTrue(
            all((stored[d]["split_ratio"], stored[d]["cash_dividend"]) == (1, 0) for d in untouched)
        )

    def test_ambiguous_action_blocks_only_that_bar(self):
        fake = self.market(
            actions={
                "forward_splits": [split("ABC", "2026-03-12", 2, 1)],
                "cash_dividends": [dividend("ABC", "2026-03-12", 0.25)],
            }
        )
        result = self.fetch(fake)
        self.assertEqual(result["added"], 5)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["symbol"], "ABC")
        self.assertEqual(result["errors"][0]["session"], "2026-03-12")
        self.assertIn("dividend", result["errors"][0]["error"])
        self.assertNotIn("2026-03-12", self.stored("ABC"))

    def test_malformed_action_blocks_that_symbol_only(self):
        fake = self.market(actions={"forward_splits": [split("ABC", "2026-03-12", 2, 0)]})
        result = self.fetch(fake)
        self.assertEqual(self.stored("ABC"), {})
        self.assertEqual(len(self.stored("SPY")), 3)
        self.assertEqual([e["symbol"] for e in result["errors"]], ["ABC"] * 3)
        self.assertIn("corporate actions", result["errors"][0]["error"])

    def test_bad_duplicate_and_off_calendar_bars_are_errors(self):
        abc = series()
        abc[-2] = raw_bar(date(2026, 3, 12), c=-1)
        abc.append(raw_bar(date(2026, 3, 13), 90.0))
        abc.append(raw_bar(date(2026, 3, 7)))  # A Saturday inside the requested window.
        fake = self.market(bars={"ABC": abc, "SPY": series(price=500.0)})
        result = self.fetch(fake, lookback_sessions=6)
        problems = {(e["symbol"], e.get("session")): e["error"] for e in result["errors"]}
        self.assertEqual(len(problems), 3)
        self.assertIn(("ABC", None), problems)  # Unreadable, so its session is unknown.
        self.assertIn("more than one", problems[("ABC", "2026-03-13")])
        self.assertIn("not a session", problems[("ABC", "2026-03-07")])
        self.assertEqual(
            list(self.stored("ABC")), ["2026-03-06", "2026-03-09", "2026-03-10", "2026-03-11"]
        )
        self.assertEqual(result["missing"], 1)  # 2026-03-12 had no usable bar.
        self.assertEqual(result["added"], 10)

    def test_missing_sessions_are_counted(self):
        abc = [row for row in series() if not row["t"].startswith("2026-03-12")]
        fake = self.market(bars={"ABC": abc, "SPY": series(price=500.0)})
        result = self.fetch(fake)
        self.assertEqual((result["added"], result["missing"], result["errors"]), (5, 1, []))

    def test_rejected_symbol_list_is_recorded_and_later_chunks_continue(self):
        fake = self.market(
            bars={"ABC": series(), "DEF": series(), "SPY": series(price=500.0)},
            fail={"/v2/stocks/bars": lambda q: http_error(400) if q["symbols"] == "ABC" else None},
        )
        with patch.object(bars, "MAX_SYMBOLS_PER_REQUEST", 1):
            result = self.fetch(fake, symbols=("ABC", "DEF"))
        self.assertEqual(result["added"], 6)
        self.assertEqual(result["errors"][0]["symbols"], ["ABC"])
        self.assertIn("400", result["errors"][0]["error"])
        self.assertEqual(len(self.stored("DEF")), 3)

    def test_systemic_failure_stops_and_reports_partial_progress(self):
        fake = self.market(
            bars={"ABC": series(), "DEF": series(), "SPY": series(price=500.0)},
            fail={"/v2/stocks/bars": lambda q: http_error(429) if q["symbols"] == "ABC" else None},
        )
        with patch.object(bars, "MAX_SYMBOLS_PER_REQUEST", 1):
            with self.assertRaises(bars.BarsError) as caught:
                self.fetch(fake, symbols=("ABC", "DEF"))
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.partial["added"], 3)
        self.assertEqual(len(self.stored("SPY")), 3)
        self.assertEqual(self.stored("DEF"), {})
        self.assertNotIn("DEF", {q["symbols"] for q in fake.queries("/v2/stocks/bars")})

    def test_calendar_failure_stores_nothing(self):
        fake = self.market(fail={"/v2/calendar": lambda q: http_error(503)})
        with self.assertRaises(bars.BarsError):
            self.fetch(fake)
        self.assertEqual(self.ledger.counts(), {})

    def test_refuses_a_ledger_with_other_benchmark_provenance(self):
        historical = normalize_bar(
            {
                "symbol": "SPY",
                "session": "2026-03-02",
                "open_at": "2026-03-02T14:30:00Z",
                "close_at": "2026-03-02T21:00:00Z",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            }
        )
        self.ledger.put("bars", historical["id"], historical)
        fake = self.market()
        with self.assertRaisesRegex(bars.BarsError, "separate ledger"):
            self.fetch(fake)
        self.assertEqual(fake.calls, [])

    def test_option_bounds(self):
        fake = self.market()
        for lookback in (0, bars.MAX_LOOKBACK_SESSIONS + 1, True, 2.0, "3"):
            with self.subTest(lookback=lookback), self.assertRaises(bars.BarsError):
                self.fetch(fake, lookback_sessions=lookback)
        with self.assertRaises(bars.BarsError):
            self.fetch(fake, feed="iex")
        self.assertEqual(fake.calls, [])

    def test_a_concurrent_first_receipt_is_kept(self):
        rival = forward_bar("ABC", date(2026, 3, 13), 42.0)

        class RacingLedger(Ledger):
            def put(self, kind, identity, payload):
                if identity == rival["id"] and self.get(kind, identity) is None:
                    super().put(kind, identity, rival)
                return super().put(kind, identity, payload)

        ledger = RacingLedger(":memory:")
        self.addCleanup(ledger.db.close)
        result = bars.fetch_forward(
            ledger, ["ABC"], now=NOW, lookback_sessions=1, transport=self.market()
        )
        self.assertEqual((result["added"], result["skipped"], result["errors"]), (1, 1, []))
        self.assertEqual(ledger.get("bars", rival["id"]), rival)

    def test_results_never_contain_keys(self):
        fake = self.market(fail={"/v2/stocks/bars": lambda q: http_error(400)})
        result = self.fetch(fake)
        self.assertNoSecrets(result)
        self.assertNoSecrets(self.ledger.all("bars"))


class FetchHistoricalTests(AlpacaTestCase):
    def test_close_time_availability_in_a_research_ledger(self):
        fake = self.market()
        result = bars.fetch_historical(
            self.ledger, ["ABC"], date(2026, 3, 9), date(2026, 3, 13), transport=fake
        )
        self.assertEqual(
            result, {"symbols": 2, "added": 10, "skipped": 0, "missing": 0, "errors": []}
        )
        bar = self.stored("ABC")["2026-03-09"]
        self.assertEqual(bar["mode"], "historical")
        # P0-15: historical availability is close + SETTLE_DELAY, not the close itself.
        self.assertEqual(instant(bar["available_at"]), instant(bar["close_at"]) + bars.SETTLE_DELAY)
        self.assertEqual(
            fake.queries("/v2/calendar"), [{"start": "2026-02-23", "end": "2026-03-13"}]
        )
        self.assertEqual(
            list(self.stored("SPY")),
            [d.isoformat() for d in weekdays(date(2026, 3, 9), date(2026, 3, 13))],
        )

    def test_unfinished_sessions_are_excluded(self):
        self.clock("2026-03-13T20:19:00.000000Z")
        result = bars.fetch_historical(
            self.ledger, ["ABC"], date(2026, 3, 9), date(2026, 3, 20), transport=self.market()
        )
        self.assertEqual(result["added"], 8)
        self.assertNotIn("2026-03-13", self.stored("ABC"))

    def test_refuses_forward_bars_and_the_forward_ledger(self):
        bar = forward_bar("SPY", date(2026, 3, 2), 500.0)
        self.ledger.put("bars", bar["id"], bar)
        fake = self.market()
        with self.assertRaisesRegex(bars.BarsError, "separate ledger"):
            bars.fetch_historical(
                self.ledger, ["ABC"], date(2026, 3, 9), date(2026, 3, 13), transport=fake
            )
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {paths.HOME_ENV: home}):
            with Ledger(paths.ledger_path()) as forward:
                with self.assertRaisesRegex(bars.BarsError, "research ledger"):
                    bars.fetch_historical(
                        forward, ["ABC"], date(2026, 3, 9), date(2026, 3, 13), transport=fake
                    )
            with Ledger(Path(home) / "research.sqlite") as research:
                result = bars.fetch_historical(
                    research, ["ABC"], date(2026, 3, 9), date(2026, 3, 13), transport=fake
                )
            self.assertEqual(result["added"], 10)
        self.assertEqual(len(fake.queries("/v2/calendar")), 1)

    def test_long_ranges_are_paged(self):
        fake = self.market(page_size=40)
        result = bars.fetch_historical(
            self.ledger, ["ABC"], date(2026, 1, 2), date(2026, 3, 13), transport=fake
        )
        sessions = len(weekdays(date(2026, 1, 2), date(2026, 3, 13)))
        self.assertEqual(result["added"], 2 * sessions)
        self.assertGreater(len(fake.queries("/v2/stocks/bars")), 1)
        prices = [bar["close"] for bar in self.stored("ABC").values()]
        self.assertTrue(all(math.isfinite(p) for p in prices))


class BarAvailabilityParityTests(AlpacaTestCase):
    """P0-15: historical bars become visible no earlier than a forward run could store them."""

    def test_one_settle_delay_shared_by_forward_and_historical_paths(self):
        from jevtrader import market

        self.assertIs(bars.SETTLE_DELAY, market.SETTLE_DELAY)
        self.assertEqual(market.SETTLE_DELAY, timedelta(minutes=20))

    def test_historical_bars_available_at_close_plus_settle_delay(self):
        bars.fetch_historical(
            self.ledger, ["ABC"], date(2026, 3, 9), date(2026, 3, 13), transport=self.market()
        )
        for bar in self.stored("ABC").values():
            with self.subTest(session=bar["session"]):
                self.assertEqual(
                    instant(bar["available_at"]), instant(bar["close_at"]) + bars.SETTLE_DELAY
                )

    def test_daily_bars_refuses_a_session_that_may_be_unfinished(self):
        fake = self.market()
        # 16:19 EDT: the Friday session closed but has not settled for 20 minutes.
        self.clock("2026-03-13T20:19:59.000000Z")
        with self.assertRaisesRegex(bars.BarsError, "unfinished"):
            bars.daily_bars(["ABC"], date(2026, 3, 9), date(2026, 3, 13), transport=fake)
        with self.assertRaisesRegex(bars.BarsError, "unfinished"):
            bars.daily_bars(["ABC"], date(2026, 3, 9), date(2026, 3, 16), transport=fake)
        self.assertEqual(fake.calls, [])
        result = bars.daily_bars(["ABC"], date(2026, 3, 9), date(2026, 3, 12), transport=fake)
        self.assertEqual(result["ABC"][-1]["session"], "2026-03-12")

    def test_daily_bars_settle_boundary_is_inclusive(self):
        self.clock("2026-03-13T20:20:00.000000Z")  # exactly 16:00 EDT + 20 minutes
        result = bars.daily_bars(
            ["ABC"], date(2026, 3, 9), date(2026, 3, 13), transport=self.market()
        )
        self.assertEqual(result["ABC"][-1]["session"], "2026-03-13")

    def test_daily_bars_boundary_uses_eastern_time_across_dst(self):
        # 2026-03-06 is before the DST change (EST, UTC-5): 16:20 EST is 21:20Z.
        fake = self.market()
        self.clock("2026-03-06T21:19:59.000000Z")
        with self.assertRaisesRegex(bars.BarsError, "unfinished"):
            bars.daily_bars(["ABC"], date(2026, 3, 2), date(2026, 3, 6), transport=fake)
        self.clock("2026-03-06T21:20:00.000000Z")
        result = bars.daily_bars(["ABC"], date(2026, 3, 2), date(2026, 3, 6), transport=fake)
        self.assertEqual(result["ABC"][-1]["session"], "2026-03-06")


if __name__ == "__main__":
    unittest.main()
