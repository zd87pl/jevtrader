"""Offline tests for universe-wide EDGAR collection; every SEC response is faked."""

import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from jevtrader import feeds, paths, sec
from jevtrader.store import Ledger

UA = "Research contact@research.test"
NOW = datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc)  # Friday 17:00 ET
FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb="
    "&owner=include&start=0&count=100&output=atom"
)
TICKERS = "https://www.sec.gov/files/company_tickers.json"
ABC, XYZ, GOOG, UNMAPPED = "0000123456", "0000654321", "0001652044", "0000999999"
TICKER_JSON = {
    "0": {"cik_str": 123456, "ticker": "ABC", "title": "ABC Corp"},
    "1": {"cik_str": 654321, "ticker": "XYZ", "title": "XYZ Inc"},
    "2": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet"},
    "3": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet"},
}
KEYS = ("accessionNumber", "form", "primaryDocument", "acceptanceDateTime", "items")


def accession(n: int) -> str:
    return f"0000123456-26-{n:06d}"


def base(cik: str, number: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{number.replace('-', '')}/"


def submissions_url(cik: str) -> str:
    return f"https://data.sec.gov/submissions/CIK{cik}.json"


def index_url(day: date) -> str:
    quarter = (day.month - 1) // 3 + 1
    return (
        f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{quarter}/"
        f"form.{day:%Y%m%d}.idx"
    )


class Filing:
    """One filing's submissions row and archive documents."""

    def __init__(
        self,
        cik,
        number,
        *,
        acceptance="2026-09-25T16:30:00-04:00",
        items="7.01,9.01",
        form="8-K",
        text="<p>Operating update.</p>",
    ):
        self.cik, self.accession, self.form = cik, number, form
        self.row = (number, form, "cover.htm", acceptance, items)
        root = base(cik, number)
        self.id = f"sec:{number}:ex991.htm"
        self.documents = {
            root + "index.json": {
                "directory": {"item": [{"name": "cover.htm"}, {"name": "ex991.htm"}]}
            },
            root + "ex991.htm": text,
        }


def submissions(cik, *filings):
    rows = [filing.row for filing in filings]
    recent = {key: [row[i] for row in rows] for i, key in enumerate(KEYS)}
    return {"cik": str(int(cik)), "filings": {"recent": recent}}


def entry(cik, number, *, form="8-K", items=("7.01", "9.01"), updated=None, title=None):
    summary = f"<b>Filed:</b> 2026-09-25 <b>AccNo:</b> {number} <b>Size:</b> 25 KB"
    summary += "".join(f"<br>Item {item}: Description" for item in items)
    return (
        f"<entry><title>{title or f'{form} - Company ({cik}) (Filer)'}</title>"
        f'<link rel="alternate" type="text/html" href="{base(cik, number)}{number}-index.htm"/>'
        f'<summary type="html">{escape(summary)}</summary>'
        f"<updated>{updated or '2026-09-25T16:30:05-04:00'}</updated>"
        f'<category scheme="https://www.sec.gov/" label="form type" term="{form}"/>'
        f"<id>urn:tag:sec.gov,2008:accession-number={number}</id></entry>"
    )


def atom(*entries):
    return (
        '<?xml version="1.0" encoding="ISO-8859-1" ?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>Latest Filings</title>'
        "<updated>2026-09-25T16:35:01-04:00</updated>" + "".join(entries) + "</feed>"
    )


def form_index(*rows):
    header = (
        "Description:           Daily Index of EDGAR Dissemination Feed by Form Type\n"
        "Last Data Received:    September 25, 2026\n"
        "Comments:              webmaster@sec.gov\n"
        "Anonymous FTP:         ftp://ftp.sec.gov/edgar/\n \n \n"
        "Form Type   Company Name                                                  CIK"
        "         Date Filed  File Name\n" + "-" * 140 + "\n"
    )
    lines = [
        f"{form:<12}{company:<62}{int(cik):<12}{filed:<12}edgar/data/{int(cik)}/{number}.txt"
        for form, company, cik, filed, number in rows
    ]
    return header + "\n".join(lines) + "\n"


class Response(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url


class FakeSEC:
    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []
        self.elapsed = 0.0

    def add(self, *filings, subs=True):
        for filing in filings:
            self.responses.update(filing.documents)
        if subs:
            for cik in {filing.cik for filing in filings}:
                self.responses[submissions_url(cik)] = submissions(
                    cik, *[f for f in filings if f.cik == cik]
                )

    def clock(self):
        return self.elapsed

    def sleep(self, duration):
        self.elapsed += duration

    def transport(self, request, *, timeout):
        url = request.full_url
        self.calls.append((url, self.elapsed, request.get_header("User-agent")))
        if url not in self.responses:
            raise AssertionError(f"Unexpected SEC request: {url}")
        value = self.responses[url]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        if isinstance(value, str):
            value = value.encode("latin-1" if url.endswith(".idx") else "utf-8")
        return Response(value, url)

    def urls(self):
        return [call[0] for call in self.calls]


def http_error(url, code):
    return HTTPError(url, code, "error", {}, None)


class FeedsCase(unittest.TestCase):
    now = NOW

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.net = FakeSEC()
        limiter = sec._RateLimiter(self.net.clock, self.net.sleep)
        for patcher in [
            patch.dict(os.environ, {paths.HOME_ENV: self.home.name}),
            patch.object(sec, "_DEFAULT_LIMITER", limiter),
            patch.object(sec, "_utc_now", lambda: self.now),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.db.close)


class LatestFeedTests(FeedsCase):
    def test_parses_8k_entries_dedupes_and_keeps_feed_items(self):
        self.net.responses[FEED] = atom(
            entry(ABC, accession(1)),
            entry(ABC, accession(1)),
            entry(XYZ, accession(2), form="8-K/A", items=()),
            entry(GOOG, accession(3), form="10-K"),
            entry(GOOG, accession(4), form="8-K12B"),
            entry(XYZ, accession(5), items=("2.02", "9.01"), updated="2026-09-25T20:00:00Z"),
        )
        rows = feeds.latest_8k(UA, transport=self.net.transport)
        self.assertEqual(
            rows,
            [
                {
                    "cik": ABC,
                    "accession": accession(1),
                    "form": "8-K",
                    "updated": "2026-09-25T20:30:05Z",
                    "items": ["7.01", "9.01"],
                },
                {
                    "cik": XYZ,
                    "accession": accession(2),
                    "form": "8-K/A",
                    "updated": "2026-09-25T20:30:05Z",
                    "items": [],
                },
                {
                    "cik": XYZ,
                    "accession": accession(5),
                    "form": "8-K",
                    "updated": "2026-09-25T20:00:00Z",
                    "items": ["2.02", "9.01"],
                },
            ],
        )
        self.assertEqual(self.net.calls, [(FEED, 0.0, UA)])

    def test_malformed_entries_are_skipped(self):
        self.net.responses[FEED] = atom(
            entry(ABC, accession(1), title="8-K - No CIK here (Filer)"),
            entry("0000000000", accession(2)),
            entry(ABC, accession(3), updated="2026-09-25"),
            entry(ABC, "not-an-accession"),
            "<entry><title>8-K - Company (0000123456) (Filer)</title></entry>",
            entry(ABC, accession(6)),
        )
        rows = feeds.latest_8k(UA, transport=self.net.transport)
        self.assertEqual([row["accession"] for row in rows], [accession(6)])

    def test_count_is_bounded_to_the_approved_values(self):
        self.net.responses[FEED.replace("100", "10")] = atom(
            *[entry(ABC, accession(n)) for n in range(1, 15)]
        )
        self.assertEqual(len(feeds.latest_8k(UA, transport=self.net.transport, count=10)), 10)
        for count in [0, 5, 50, 101, True, "100"]:
            with self.subTest(count=count), self.assertRaises(ValueError):
                feeds.latest_8k(UA, transport=self.net.transport, count=count)
        self.assertEqual(len(self.net.calls), 1)

    def test_unsafe_or_foreign_payloads_fail_closed(self):
        for payload in [
            '<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY x "y">]>'
            '<feed xmlns="http://www.w3.org/2005/Atom">&x;</feed>',
            "<html><body>Request rate threshold exceeded</body></html>",
            # A byte-level DTD check misses UTF-16, which the XML parser still reads.
            (
                '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE feed [<!ENTITY x "y">]>'
                '<feed xmlns="http://www.w3.org/2005/Atom">&x;</feed>'
            ).encode("utf-16"),
            # Without a BOM it even decodes as UTF-8, and expat still detects UTF-16.
            *(
                (
                    '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE feed [<!ENTITY x "y">]>'
                    '<feed xmlns="http://www.w3.org/2005/Atom">&x;</feed>'
                ).encode(codec)
                for codec in ("utf-16-le", "utf-16-be")
            ),
            "not xml",
            '<feed xmlns="http://example.test/other"></feed>',
        ]:
            self.net.responses[FEED] = payload
            with self.subTest(payload=payload[:40]), self.assertRaises(sec.SECError):
                feeds.latest_8k(UA, transport=self.net.transport)

    def test_user_agent_needs_contact_before_any_request(self):
        for agent in ["", "Bot", "Bot a@b.test\r\nX: y"]:
            with self.subTest(agent=agent), self.assertRaises(sec.SECError):
                feeds.latest_8k(agent, transport=self.net.transport)
        self.assertEqual(self.net.calls, [])


class TickerMapTests(FeedsCase):
    def test_first_listed_ticker_per_cik_with_ten_digit_keys(self):
        data = dict(TICKER_JSON)
        data.update(
            {
                "10": {"cik_str": "777", "ticker": "late", "title": "Order by rank"},
                "9": {"cik_str": 777, "ticker": "EARLY", "title": "Order by rank"},
                "4": {"cik_str": True, "ticker": "BOOL"},
                "5": {"cik_str": 0, "ticker": "ZERO"},
                "6": {"cik_str": 42, "ticker": "BAD TICKER"},
                "7": {"cik_str": "4a", "ticker": "HEX"},
                "8": ["not", "an", "entry"],
                "11": {"cik_str": 43, "ticker": "WAYTOOLONGTICKER"},
            }
        )
        self.net.responses[TICKERS] = data
        mapping = feeds.ticker_map(UA, transport=self.net.transport)
        self.assertEqual(mapping, {ABC: "ABC", XYZ: "XYZ", GOOG: "GOOGL", "0000000777": "EARLY"})
        self.assertEqual(len(self.net.calls), 1)

    def test_unusable_maps_fail_closed(self):
        for payload in [{}, [], "nope", {"0": {"cik_str": 1}}]:
            self.net.responses[TICKERS] = payload
            with self.subTest(payload=payload), self.assertRaises(sec.SECError):
                feeds.ticker_map(UA, transport=self.net.transport)

    def test_ticker_map_may_exceed_the_document_byte_bound(self):
        data = dict(TICKER_JSON)
        data["0"] = {**data["0"], "title": "x" * (sec.MAX_RESPONSE_BYTES + 10)}
        self.net.responses[TICKERS] = data
        self.assertEqual(feeds.ticker_map(UA, transport=self.net.transport)[ABC], "ABC")


class DailyIndexTests(FeedsCase):
    DAY = date(2026, 9, 25)

    def test_parses_only_8k_rows_with_accessions(self):
        self.net.responses[index_url(self.DAY)] = form_index(
            ("10-K", "APPLE INC", "320193", "20260925", "0000320193-26-000001"),
            ("8-K", "3M CO", "66740", "20260925", "0000066740-26-000002"),
            ("8-K/A", "COMPANY 2 INC", "123456", "20260925", accession(3)),
            ("8-K12B", "NEW HOLDCO", "123457", "20260925", "0000123457-26-000004"),
            ("SC 13G", "8-K FUND LP", "123458", "20260925", "0000123458-26-000005"),
            ("8-K", "3M CO", "66740", "20260925", "0000066740-26-000002"),
            ("8-K", "ZERO CIK", "0", "20260925", "0000000000-26-000006"),
        )
        rows = feeds.daily_index(UA, self.DAY, transport=self.net.transport)
        self.assertEqual(
            rows,
            [
                {
                    "cik": "0000066740",
                    "accession": "0000066740-26-000002",
                    "form": "8-K",
                    "company": "3M CO",
                    "date_filed": "2026-09-25",
                },
                {
                    "cik": ABC,
                    "accession": accession(3),
                    "form": "8-K/A",
                    "company": "COMPANY 2 INC",
                    "date_filed": "2026-09-25",
                },
            ],
        )
        self.assertEqual(
            self.net.urls(),
            ["https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260925.idx"],
        )

    def test_quarter_boundaries_build_approved_urls(self):
        for day, quarter in [
            (date(2026, 3, 31), "QTR1"),
            (date(2026, 4, 1), "QTR2"),
            (date(2026, 6, 30), "QTR2"),
            (date(2026, 7, 1), "QTR3"),
            (date(2026, 12, 31), "QTR4"),
        ]:
            with self.subTest(day=day):
                url = feeds._index_url(day)
                self.assertIn(f"/{day.year}/{quarter}/form.{day:%Y%m%d}.idx", url)
                sec._validate_url(url)

    def test_weekends_make_no_request_and_missing_index_is_empty(self):
        self.assertEqual(feeds.daily_index(UA, date(2026, 9, 26), transport=self.net.transport), [])
        self.assertEqual(self.net.calls, [])
        self.net.responses[index_url(self.DAY)] = http_error(index_url(self.DAY), 404)
        self.assertEqual(feeds.daily_index(UA, self.DAY, transport=self.net.transport), [])
        self.net.responses[index_url(self.DAY)] = http_error(index_url(self.DAY), 503)
        with self.assertRaises(HTTPError) as caught:
            feeds.daily_index(UA, self.DAY, transport=self.net.transport)
        caught.exception.close()

    def test_malformed_index_and_bad_days_fail_closed(self):
        self.net.responses[index_url(self.DAY)] = "<html>Access denied</html>"
        with self.assertRaises(sec.SECError):
            feeds.daily_index(UA, self.DAY, transport=self.net.transport)
        calls = len(self.net.calls)
        for day in [datetime(2026, 9, 25, 12), "2026-09-25", None, date(1993, 12, 31)]:
            with self.subTest(day=day), self.assertRaises(ValueError):
                feeds.daily_index(UA, day, transport=self.net.transport)
        self.assertEqual(len(self.net.calls), calls)


class AssumedAvailabilityTests(unittest.TestCase):
    def test_conservative_first_seen_rule(self):
        for accepted, expected in [
            ("2026-09-24T10:00:00-04:00", "2026-09-24T14:15:00Z"),  # weekday daytime
            ("2026-09-24T06:00:00-04:00", "2026-09-24T10:15:00Z"),  # EDGAR opens
            ("2026-09-24T05:59:59-04:00", "2026-09-24T10:00:00Z"),  # same day 06:00
            ("2026-09-24T17:29:00-04:00", "2026-09-24T21:44:00Z"),
            ("2026-09-24T17:30:00-04:00", "2026-09-25T10:00:00Z"),  # at 17:30 -> next day
            ("2026-09-25T18:00:00-04:00", "2026-09-28T10:00:00Z"),  # Friday -> Monday
            ("2026-09-26T12:00:00-04:00", "2026-09-28T10:00:00Z"),  # Saturday
            ("2026-09-27T05:00:00-04:00", "2026-09-28T10:00:00Z"),  # Sunday early
            ("2026-01-06T17:45:00-05:00", "2026-01-07T11:00:00Z"),  # winter offset
            ("2026-03-06T18:00:00-05:00", "2026-03-09T10:00:00Z"),  # across DST start
            ("2026-09-24T10:00:00", "2026-09-24T14:15:00Z"),  # naive means Eastern
            ("2026-09-24T16:30:00.000Z", "2026-09-24T20:45:00Z"),  # "Z" read as Eastern
            ("2026-09-24T18:01:14.000Z", "2026-09-25T10:00:00Z"),
            ("2026-09-24T02:00:00Z", "2026-09-24T10:00:00Z"),
        ]:
            with self.subTest(accepted=accepted):
                self.assertEqual(feeds.assumed_first_seen(accepted), expected)

    def test_never_before_acceptance_and_rejects_dates(self):
        accepted = "2026-09-24T16:00:00-04:00"
        assumed = datetime.fromisoformat(feeds.assumed_first_seen(accepted).replace("Z", "+00:00"))
        self.assertGreater(assumed, datetime.fromisoformat(accepted))
        for value in ["2026-09-24", "", "later"]:
            with self.subTest(value=value), self.assertRaises(sec.SECError):
                feeds.assumed_first_seen(value)


class PollTests(FeedsCase):
    def setUp(self):
        super().setUp()
        self.clock = [0.0]
        self.memory = feeds.PollMemory(clock=lambda: self.clock[0])
        self.net.responses[TICKERS] = TICKER_JSON

    def poll(self, symbols=None, **kwargs):
        kwargs.setdefault("memory", self.memory)
        return feeds.poll(self.ledger, UA, symbols=symbols, transport=self.net.transport, **kwargs)

    def test_collects_new_watched_filings_with_actual_receipt(self):
        filing = Filing(ABC, accession(1))
        self.net.add(filing)
        self.net.responses[FEED] = atom(
            entry(ABC, accession(1)),
            entry(UNMAPPED, accession(2)),
            entry(XYZ, accession(3)),
            entry(ABC, accession(4), items=("2.02", "7.01")),
        )
        result = self.poll({"abc"})
        self.assertEqual(result["added"], [filing.id])
        self.assertEqual((result["seen"], result["new"], result["errors"]), (4, 4, []))
        self.assertEqual(
            result["skipped"],
            {
                "not_qualifying": 1,
                "unmapped": 1,
                "not_watched": 1,
                "not_indexed": 0,
                "failed_before": 0,
                "deferred": 0,
            },
        )
        self.assertIsNone(result["stopped"])
        self.assertEqual(result["requests"], 5)
        self.assertEqual(self.net.urls(), [FEED, TICKERS, submissions_url(ABC), *filing.documents])
        record = self.ledger.get("disclosures", filing.id)
        self.assertEqual(record["mode"], "forward")
        self.assertEqual(record["symbol"], "ABC")
        self.assertEqual(record["first_seen_at"], "2026-09-25T21:00:00.000000Z")
        self.assertEqual(record["accepted_at"], "2026-09-25T20:30:00Z")
        self.assertIs(record["after_hours"], False)
        self.assertEqual(record["text"], "Operating update.")
        self.assertNotIn("first_seen_basis", record)

    def test_dotted_share_class_watchlist_matches_sec_tickers(self):
        # SEC's ticker map writes BRK-B; users and Alpaca write BRK.B.
        self.net.responses[TICKERS] = {
            "0": {"cik_str": 123456, "ticker": "BRK-B", "title": "Berkshire"},
            "1": {"cik_str": 123456, "ticker": "BRK-A", "title": "Berkshire"},
        }
        filing = Filing(ABC, accession(1))
        self.net.add(filing)
        self.net.responses[FEED] = atom(entry(ABC, accession(1)))
        result = self.poll({"brk.b"})
        self.assertEqual(result["added"], [filing.id])
        self.assertEqual(self.ledger.get("disclosures", filing.id)["symbol"], "BRK-B")

    def test_known_accessions_cost_no_requests(self):
        self.net.add(Filing(ABC, accession(1)))
        self.net.responses[FEED] = atom(entry(ABC, accession(1)))
        self.assertEqual(len(self.poll()["added"]), 1)
        self.net.calls.clear()
        again = self.poll()
        self.assertEqual((again["seen"], again["new"], again["added"]), (1, 0, []))
        self.assertEqual(self.net.urls(), [FEED])

    def test_one_bad_filing_is_recorded_and_the_batch_continues(self):
        bad, good = Filing(ABC, accession(1)), Filing(XYZ, accession(2))
        self.net.add(bad, good)
        self.net.responses[base(ABC, accession(1)) + "index.json"] = {"directory": {}}
        self.net.responses[FEED] = atom(entry(XYZ, accession(2)), entry(ABC, accession(1)))
        result = self.poll()
        self.assertEqual(result["added"], [good.id])
        self.assertEqual(len(result["errors"]), 1)
        error = result["errors"][0]
        self.assertEqual(
            {k: error[k] for k in ("accession", "cik", "symbol")},
            {"accession": accession(1), "cik": ABC, "symbol": "ABC"},
        )
        self.assertTrue(error["error"].startswith("SECError: "))
        self.assertLessEqual(len(error["error"]), 300)
        self.assertIsNone(result["stopped"])

    def test_throttling_outages_and_network_failures_stop_the_batch(self):
        url = submissions_url(ABC)
        for failure, stops in [
            (http_error(url, 429), True),
            (http_error(url, 403), True),
            (http_error(url, 503), True),
            (URLError("offline"), True),
            (TimeoutError("timed out"), True),
            (http_error(url, 404), False),
        ]:
            with self.subTest(failure=failure):
                self.ledger = Ledger(":memory:")
                self.memory = feeds.PollMemory()
                self.net = FakeSEC({TICKERS: TICKER_JSON})
                self.net.add(Filing(ABC, accession(1)), Filing(XYZ, accession(2)))
                self.net.responses[url] = failure
                self.net.responses[FEED] = atom(entry(XYZ, accession(2)), entry(ABC, accession(1)))
                result = self.poll()
                self.assertEqual(len(result["errors"]), 1)
                self.assertEqual(result["stopped"] is not None, stops)
                self.assertEqual(submissions_url(XYZ) in self.net.urls(), not stops)
                self.ledger.db.close()

    def test_feed_failure_propagates(self):
        self.net.responses[FEED] = http_error(FEED, 503)
        with self.assertRaises(HTTPError) as caught:
            self.poll()
        caught.exception.close()

    def test_unindexed_filing_is_retried_then_given_up(self):
        self.net.responses[FEED] = atom(entry(ABC, accession(1)))
        self.net.responses[submissions_url(ABC)] = submissions(ABC, Filing(ABC, accession(9)))
        with patch.object(feeds, "MAX_FILING_ATTEMPTS", 2):
            for expected in ["not_indexed", "not_indexed", "failed_before"]:
                self.net.calls.clear()
                result = self.poll()
                self.assertEqual(result["skipped"][expected], 1)
                self.assertEqual(result["errors"], [])
            self.assertEqual(self.net.urls(), [FEED])

    def test_non_qualifying_submission_is_remembered(self):
        self.net.add(Filing(ABC, accession(1), items="2.02,9.01"))
        self.net.responses[FEED] = atom(entry(ABC, accession(1), items=()))
        first = self.poll()
        self.assertEqual(first["skipped"]["not_qualifying"], 1)
        self.assertIn(submissions_url(ABC), self.net.urls())
        self.net.calls.clear()
        second = self.poll()
        self.assertEqual(second["skipped"]["not_qualifying"], 1)
        self.assertEqual(self.net.urls(), [FEED])

    def test_max_filings_processes_oldest_first_and_defers_the_rest(self):
        filings = [Filing(ABC, accession(n)) for n in (1, 2, 3)]
        self.net.add(*filings)
        self.net.responses[FEED] = atom(*[entry(ABC, f.accession) for f in reversed(filings)])
        result = self.poll(max_filings=1)
        self.assertEqual(result["added"], [filings[0].id])
        self.assertEqual(result["skipped"]["deferred"], 2)
        self.assertLessEqual(result["requests"], 2 + feeds.REQUESTS_PER_FILING)
        self.assertEqual(self.poll(max_filings=2)["added"], [f.id for f in filings[1:]])

    def test_universe_and_coregistrant_symbol_choice(self):
        shared = Filing(GOOG, accession(1))
        self.net.add(shared)
        self.net.responses[FEED] = atom(entry(UNMAPPED, accession(1)), entry(GOOG, accession(1)))
        result = self.poll({"GOOG"})
        self.assertEqual(result["seen"], 1)
        self.assertEqual(self.ledger.get("disclosures", shared.id)["symbol"], "GOOG")
        self.ledger = Ledger(":memory:")
        self.assertEqual(len(self.poll(None)["added"]), 1)
        self.assertEqual(self.ledger.get("disclosures", shared.id)["symbol"], "GOOGL")
        self.ledger.db.close()

    def test_empty_watchlist_fetches_nothing_per_filing(self):
        self.net.responses[FEED] = atom(entry(ABC, accession(1)))
        result = self.poll(set())
        self.assertEqual(result["skipped"]["not_watched"], 1)
        self.assertEqual(self.net.urls(), [FEED, TICKERS])

    def test_ticker_map_is_cached_until_its_ttl(self):
        self.net.add(*[Filing(ABC, accession(n)) for n in (1, 2, 3)])
        for number, advance, fetched in [(1, 0, True), (2, 60, False), (3, 6 * 3600, True)]:
            self.clock[0] += advance
            self.net.calls.clear()
            self.net.responses[FEED] = atom(entry(ABC, accession(number)))
            self.poll()
            with self.subTest(number=number):
                self.assertEqual(TICKERS in self.net.urls(), fetched)

    def test_future_receipt_is_refused_by_the_ledger_and_reported(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.net.add(Filing(ABC, accession(1)))
        self.net.responses[FEED] = atom(entry(ABC, accession(1)))
        result = self.poll()
        self.assertEqual(result["added"], [])
        self.assertIn("future", result["errors"][0]["error"])

    def test_invalid_arguments_make_no_request(self):
        for kwargs in [
            {"max_filings": 0},
            {"max_filings": feeds.MAX_POLL_FILINGS + 1},
            {"max_filings": True},
            {"max_filings": "5"},
            {"symbols": "ABC"},
            {"symbols": {"not a symbol"}},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.poll(**kwargs)
        with self.assertRaises(sec.SECError):
            feeds.poll(self.ledger, "Bot", symbols=None, transport=self.net.transport)
        self.assertEqual(self.net.calls, [])

    def test_refuses_the_research_ledger(self):
        with Ledger(paths.research_ledger_path()) as research:
            with self.assertRaises(ValueError):
                feeds.poll(research, UA, symbols=None, transport=self.net.transport)
        self.assertEqual(self.net.calls, [])

    def test_requests_share_the_rate_limiter(self):
        self.net.add(Filing(ABC, accession(1)))
        self.net.responses[FEED] = atom(entry(ABC, accession(1)))
        self.poll()
        times = [call[1] for call in self.net.calls]
        self.assertEqual(len(times), 5)
        self.assertTrue(all(b - a >= 0.2 for a, b in zip(times, times[1:])))


class BackfillTests(FeedsCase):
    FRIDAY, MONDAY = date(2026, 9, 18), date(2026, 9, 21)

    def setUp(self):
        super().setUp()
        self.net.responses[TICKERS] = TICKER_JSON

    def backfill(self, start=None, end=None, **kwargs):
        return feeds.backfill(
            self.ledger,
            UA,
            start or self.FRIDAY,
            end or self.MONDAY,
            transport=self.net.transport,
            **kwargs,
        )

    def index(self, day, *filings):
        rows = [(f.form, "COMPANY", f.cik, f"{day:%Y%m%d}", f.accession) for f in filings]
        self.net.responses[index_url(day)] = form_index(*rows)

    def test_collects_historical_records_with_assumed_availability(self):
        evening = Filing(ABC, accession(1), acceptance="2026-09-18T18:05:00.000Z")
        morning = Filing(ABC, accession(2), acceptance="2026-09-21T10:00:00-04:00")
        self.net.add(evening, morning)
        self.index(self.FRIDAY, evening)
        self.index(self.MONDAY, morning)
        result = self.backfill()
        self.assertEqual(result["added"], [evening.id, morning.id])
        self.assertEqual((result["days"], result["seen"], result["new"]), (2, 2, 2))
        self.assertFalse(result["truncated"])
        self.assertEqual(result["errors"], [])
        # Two indexes, the ticker map, one shared submissions file, two documents each.
        self.assertEqual(result["requests"], 8)
        self.assertEqual(self.net.urls().count(submissions_url(ABC)), 1)
        self.assertNotIn(index_url(date(2026, 9, 19)), self.net.urls())
        first = self.ledger.get("disclosures", evening.id)
        self.assertEqual(first["mode"], "historical")
        self.assertEqual(first["first_seen_basis"], "backfill_assumed")
        self.assertEqual(first["symbol_basis"], "sec_ticker_map_at_backfill")
        self.assertEqual(first["published_at"], "2026-09-18T18:05:00.000000Z")
        self.assertEqual(first["first_seen_at"], "2026-09-21T10:00:00.000000Z")
        self.assertIs(first["after_hours"], True)
        second = self.ledger.get("disclosures", morning.id)
        self.assertEqual(second["first_seen_at"], "2026-09-21T14:15:00.000000Z")
        self.assertIs(second["after_hours"], False)

    def test_refuses_the_forward_ledger(self):
        with Ledger(paths.ledger_path()) as forward:
            with self.assertRaises(ValueError):
                feeds.backfill(forward, UA, self.FRIDAY, self.MONDAY, transport=self.net.transport)
        self.ledger.disclosure(
            {
                "id": "sec:forward",
                "symbol": "ABC",
                "text": "Observed live.",
                "source_url": "https://www.sec.gov/x",
                "mode": "forward",
                "published_at": "2026-09-18T20:00:00Z",
                "first_seen_at": "2026-09-18T20:01:00Z",
            },
            imported=False,
        )
        with self.assertRaises(ValueError):
            self.backfill()
        self.assertEqual(self.net.calls, [])

    def test_invalid_ranges_make_no_request(self):
        span = feeds.MAX_BACKFILL_DAYS
        for start, end in [
            (self.MONDAY, self.FRIDAY),
            (date(2025, 1, 1), date(2025, 1, 1) + (date(2025, 1, 2) - date(2025, 1, 1)) * span),
            (datetime(2026, 9, 18), self.MONDAY),
            ("2026-09-18", self.MONDAY),
            (self.FRIDAY, date(2026, 9, 26)),  # after "today" in New York
        ]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.backfill(start, end)
        for count in [0, feeds.MAX_BACKFILL_FILINGS + 1, True]:
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.backfill(max_filings=count)
        self.assertEqual(self.net.calls, [])

    def test_max_filings_truncates_without_fetching_later_days(self):
        first, second = Filing(ABC, accession(1)), Filing(ABC, accession(2))
        self.net.add(first, second)
        self.index(self.FRIDAY, first, second)
        self.index(self.MONDAY, Filing(ABC, accession(3)))
        result = self.backfill(max_filings=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["added"]), 1)
        self.assertNotIn(index_url(self.MONDAY), self.net.urls())
        self.net.responses[index_url(self.MONDAY)] = form_index()
        again = self.backfill(max_filings=1)
        self.assertEqual((again["new"], len(again["added"]), again["truncated"]), (1, 1, False))

    def test_availability_still_in_the_future_is_skipped(self):
        self.now = datetime(2026, 9, 25, 23, 0, tzinfo=timezone.utc)  # Friday 19:00 ET
        late = Filing(ABC, accession(1), acceptance="2026-09-25T17:45:00-04:00")
        early = Filing(ABC, accession(2), acceptance="2026-09-25T09:00:00-04:00")
        self.net.add(late, early)
        day = date(2026, 9, 25)
        self.index(day, late, early)
        result = self.backfill(day, day)
        self.assertEqual(result["skipped"]["not_yet_available"], 1)
        self.assertEqual(result["added"], [early.id])

    def test_missing_filings_errors_and_throttling(self):
        absent = Filing(ABC, accession(1))
        broken = Filing(XYZ, accession(2))
        throttled = Filing(GOOG, accession(3))
        after = Filing(ABC, accession(4))
        self.net.add(broken, throttled, after)
        self.net.responses[submissions_url(ABC)] = submissions(ABC, after)
        missing = base(XYZ, accession(2)) + "ex991.htm"
        self.net.responses[missing] = http_error(missing, 404)
        self.net.responses[submissions_url(GOOG)] = http_error(submissions_url(GOOG), 429)
        self.index(self.FRIDAY, absent, broken, throttled, after)
        result = self.backfill(self.FRIDAY, self.FRIDAY)
        self.assertEqual(result["skipped"]["not_in_submissions"], 1)
        self.assertEqual([e["accession"] for e in result["errors"]], [accession(2), accession(3)])
        self.assertIn("429", result["stopped"])
        self.assertEqual(result["added"], [])
        self.assertNotIn(base(ABC, accession(4)) + "index.json", self.net.urls())

    def test_index_failures_are_reported_and_holidays_are_empty(self):
        self.net.responses[index_url(self.FRIDAY)] = http_error(index_url(self.FRIDAY), 404)
        self.net.responses[index_url(self.MONDAY)] = "garbage"
        result = self.backfill()
        self.assertEqual(result["days"], 1)
        self.assertEqual(result["errors"][0]["day"], "2026-09-21")
        self.assertIsNone(result["stopped"])
        self.net.responses[index_url(self.FRIDAY)] = http_error(index_url(self.FRIDAY), 429)
        self.net.calls.clear()
        stopped = self.backfill()
        self.assertIn("429", stopped["stopped"])
        self.assertEqual(self.net.urls(), [index_url(self.FRIDAY)])

    def test_known_and_unwatched_filings_cost_no_filing_requests(self):
        known, other = Filing(ABC, accession(1)), Filing(XYZ, accession(2))
        self.net.add(known, other)
        self.index(self.FRIDAY, known, other)
        self.backfill(self.FRIDAY, self.FRIDAY, symbols={"ABC"})
        self.net.calls.clear()
        result = self.backfill(self.FRIDAY, self.FRIDAY, symbols={"ABC"})
        self.assertEqual((result["seen"], result["new"]), (2, 1))
        self.assertEqual(result["skipped"]["not_watched"], 1)
        self.assertEqual(self.net.urls(), [index_url(self.FRIDAY), TICKERS])

    def test_research_ledger_file_is_accepted(self):
        filing = Filing(ABC, accession(1))
        self.net.add(filing)
        self.index(self.FRIDAY, filing)
        path = paths.research_ledger_path()
        with Ledger(path) as research:
            result = feeds.backfill(
                research, UA, self.FRIDAY, self.FRIDAY, transport=self.net.transport
            )
            self.assertEqual(result["added"], [filing.id])
        self.assertTrue(Path(path).is_file())


if __name__ == "__main__":
    unittest.main()
