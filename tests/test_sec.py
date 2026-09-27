"""Offline unit tests; SEC traffic requires a real contact-bearing User-Agent."""

import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError

from jevtrader import sec


CIK = "0000123456"
SUBMISSIONS = f"https://data.sec.gov/submissions/CIK{CIK}.json"
ACCESSION = "0000123456-26-000001"
BASE = "https://www.sec.gov/Archives/edgar/data/123456/000012345626000001/"
NOW = datetime(2026, 9, 27, 4, 0, tzinfo=timezone.utc)


def recent(**overrides):
    row = {
        "accessionNumber": [ACCESSION],
        "form": ["8-K"],
        "primaryDocument": ["cover.htm"],
        "acceptanceDateTime": ["2026-09-25T16:30:00-04:00"],
        "items": ["7.01,8.01,9.01"],
    }
    row.update(overrides)
    return {"filings": {"recent": row}}


class Response(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url


class FakeNetwork:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.elapsed = 0.0

    def clock(self):
        return self.elapsed

    def sleep(self, duration):
        self.elapsed += duration

    def transport(self, request, *, timeout):
        url = request.full_url
        self.calls.append((url, self.elapsed, request.get_header("User-agent")))
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, dict):
            value = json.dumps(value).encode()
        if isinstance(value, str):
            value = value.encode()
        return Response(value, url)

    def client(self, user_agent, timeout, max_requests):
        return REAL_CLIENT(
            user_agent,
            timeout,
            max_requests,
            transport=self.transport,
            clock=self.clock,
            sleep=self.sleep,
            now=lambda: NOW,
        )


REAL_CLIENT = sec._SECClient


class CollectorTests(unittest.TestCase):
    def collect(self, responses, **kwargs):
        network = FakeNetwork(responses)
        with patch.object(sec, "_SECClient", side_effect=network.client):
            result = sec.collect_disclosures(
                "123456", "abc", user_agent="Research contact@research.test", **kwargs
            )
        return result, network

    def test_prefers_exhibit_and_records_actual_observation(self):
        records, network = self.collect(
            {
                SUBMISSIONS: recent(),
                BASE + "index.json": {
                    "directory": {
                        "item": [
                            {"name": "cover.htm"},
                            {"name": "d123ex992.htm"},
                            {"name": "d123ex991.htm"},
                            {"name": "../../bad-ex99.htm"},
                        ]
                    }
                },
                BASE
                + "d123ex991.htm": "<html><style>hidden</style><p>Demand &amp; revenue.</p><script>evil()</script><p>Second paragraph.</p></html>",
            }
        )
        record = records[0]
        self.assertEqual(record["text"], "Demand & revenue.\n\nSecond paragraph.")
        self.assertEqual(record["published_at"], "2026-09-25T20:30:00Z")
        self.assertEqual(record["first_seen_at"], "2026-09-27T04:00:00Z")
        self.assertEqual(record["mode"], "forward")
        self.assertEqual(record["symbol"], "ABC")
        self.assertEqual(record["source_type"], "sec")
        self.assertEqual(record["id"], f"sec:{ACCESSION}:d123ex991.htm")
        self.assertEqual(record["document_role"], "exhibit")
        self.assertEqual(len(network.calls), 3)
        self.assertTrue(all(b[1] - a[1] >= 0.2 for a, b in zip(network.calls, network.calls[1:])))
        self.assertIn("contact@research.test", network.calls[0][2])

    def test_non_earnings_excludes_earnings_and_unknown_items(self):
        for items in ["2.02,7.01,8.01", "", None, "9.01", ["7.01"]]:
            with self.subTest(items=items):
                records, network = self.collect({SUBMISSIONS: recent(items=[items])})
                self.assertEqual(records, [])
                self.assertEqual(len(network.calls), 1)

    def test_cover_link_resolves_exhibit_without_following_external_links(self):
        records, network = self.collect(
            {
                SUBMISSIONS: recent(form=["8-K/A"]),
                BASE + "index.json": {
                    "directory": {"item": [{"name": "cover.htm"}, {"name": "press.htm"}]}
                },
                BASE
                + "cover.htm": '<a href="https://evil.test/ex99.htm">99.1</a><a href="../other/ex99.htm">99.1</a><a href="press.htm#part">Exhibit 99.1</a>',
                BASE + "press.htm": "<p>Actual operating announcement.</p>",
            }
        )
        self.assertEqual(records[0]["selection_method"], "cover_link")
        self.assertEqual(records[0]["document"], "press.htm")
        self.assertEqual(records[0]["form"], "8-K/A")
        self.assertEqual(len(network.calls), 4)

    def test_primary_fallback_is_explicit_and_plain_text_decodes(self):
        records, _ = self.collect(
            {
                SUBMISSIONS: recent(primaryDocument=["cover.txt"]),
                BASE + "index.json": {"directory": {"item": [{"name": "ex99.pdf"}]}},
                BASE + "cover.txt": "Operating update &amp; disclosure.\nNext paragraph.",
            }
        )
        self.assertEqual(records[0]["document_role"], "primary_document_fallback")
        self.assertIn("& disclosure", records[0]["text"])

    def test_naive_timestamps_use_new_york_seasonal_offset(self):
        self.assertEqual(sec._published_at("2026-07-01T12:00:00"), "2026-07-01T16:00:00Z")
        self.assertEqual(sec._published_at("2026-01-01T12:00:00"), "2026-01-01T17:00:00Z")
        self.assertEqual(sec._published_at("2026-07-01T12:00:00Z"), "2026-07-01T12:00:00Z")
        with self.assertRaises(sec.SECError):
            sec._published_at("2026-07-01")

    def test_invalid_inputs_never_make_a_request(self):
        with patch.object(sec, "_transport") as transport:
            for kwargs in [
                {"cik": "../1"},
                {"cik": "0"},
                {"cik": "1" * 11},
                {"symbol": "ABC/DEF"},
                {"user_agent": "Bot"},
                {"user_agent": "Bot a@b.test\r\nInjected: yes"},
                {"limit": 0},
                {"limit": 21},
                {"limit": True},
                {"timeout": float("nan")},
            ]:
                values = {"cik": "123456", "symbol": "ABC", "user_agent": "Bot a@b.test"}
                values.update(kwargs)
                with self.subTest(kwargs=kwargs), self.assertRaises(sec.SECError):
                    sec.collect_disclosures(**values)
            transport.assert_not_called()

    def test_malformed_json_and_parallel_arrays_fail_closed(self):
        broken = recent()
        broken["filings"]["recent"]["items"] = []
        for payload in ["not json", "[]", {}, broken]:
            with self.subTest(payload=payload), self.assertRaises(sec.SECError):
                self.collect({SUBMISSIONS: payload})

    def test_invalid_accession_document_and_timestamp_are_skipped(self):
        for changes in [
            {"accessionNumber": ["../../outside"]},
            {"primaryDocument": ["../../ex99.htm"]},
            {"acceptanceDateTime": ["yesterday"]},
        ]:
            with self.subTest(changes=changes):
                records, network = self.collect({SUBMISSIONS: recent(**changes)})
                self.assertEqual(records, [])
                self.assertEqual(len(network.calls), 1)

    def test_response_byte_bound_and_request_budget(self):
        network = FakeNetwork({SUBMISSIONS: b"x" * (sec.MAX_RESPONSE_BYTES + 1)})
        client = network.client("Bot a@b.test", 20, 1)
        with self.assertRaises(sec.SECError):
            client.get(SUBMISSIONS)
        with self.assertRaises(sec.SECError):
            client.get(SUBMISSIONS)
        self.assertEqual(len(network.calls), 1)

    def test_unapproved_urls_and_redirects_are_blocked(self):
        for url in [
            "http://data.sec.gov/submissions/CIK0000123456.json",
            "https://evil.test/ex99.htm",
            "https://www.sec.gov/Archives/edgar/data/123456/000012345626000001/../ex99.htm",
            BASE + "ex99.htm?next=https://evil.test",
            "https://www.sec.gov:443/Archives/edgar/data/123456/000012345626000001/ex99.htm",
        ]:
            with self.subTest(url=url), self.assertRaises(sec.SECError):
                sec._validate_url(url)
        with self.assertRaises(sec.SECError):
            sec._NoRedirects().redirect_request(None, None, 302, "", {}, "https://evil.test")

    def test_default_limiter_is_shared_across_collection_clients(self):
        network = FakeNetwork({SUBMISSIONS: "{}"})
        limiter = sec._RateLimiter(network.clock, network.sleep)
        with patch.object(sec, "_DEFAULT_LIMITER", limiter):
            first = REAL_CLIENT("Bot a@b.test", 20, 1, transport=network.transport)
            second = REAL_CLIENT("Bot a@b.test", 20, 1, transport=network.transport)
            first.get(SUBMISSIONS)
            second.get(SUBMISSIONS)
        self.assertGreaterEqual(network.calls[1][1] - network.calls[0][1], 0.2)

    def test_missing_directory_falls_back_but_rate_limit_error_propagates(self):
        records, _ = self.collect(
            {
                SUBMISSIONS: recent(),
                BASE + "index.json": HTTPError(BASE, 404, "Not found", {}, None),
                BASE + "cover.htm": "<p>Cover only.</p>",
            }
        )
        self.assertEqual(records[0]["document_role"], "primary_document_fallback")
        error = HTTPError(BASE, 429, "Slow down", {}, None)
        try:
            with self.assertRaises(HTTPError):
                self.collect({SUBMISSIONS: recent(), BASE + "index.json": error})
        finally:
            error.close()

    def test_malformed_directory_and_empty_text_fail_closed(self):
        for directory in [{}, {"directory": {"item": "bad"}}]:
            with self.subTest(directory=directory), self.assertRaises(sec.SECError):
                self.collect({SUBMISSIONS: recent(), BASE + "index.json": directory})
        with self.assertRaises(sec.SECError):
            self.collect(
                {
                    SUBMISSIONS: recent(),
                    BASE + "index.json": {"directory": {"item": []}},
                    BASE + "cover.htm": "<script>hidden</script>",
                }
            )

    def test_limit_stops_after_first_filing(self):
        data = recent()
        for values in data["filings"]["recent"].values():
            values.append(values[0])
        records, network = self.collect(
            {
                SUBMISSIONS: data,
                BASE + "index.json": {"directory": {"item": []}},
                BASE + "cover.htm": "<p>Text.</p>",
            },
            limit=1,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(len(network.calls), 3)


class CollectFilingTests(unittest.TestCase):
    """collect_filing is shared by collect_disclosures and the universe-wide feeds."""

    DOCS = {
        BASE + "index.json": {"directory": {"item": [{"name": "d123ex991.htm"}]}},
        BASE + "d123ex991.htm": "<p>Operating update.</p>",
    }

    def collect(self, responses=None, *, submissions=None, **kwargs):
        network = FakeNetwork({**self.DOCS, **(responses or {})})
        client = network.client("Bot a@b.test", 20, 4)
        record = sec.collect_filing(
            client, "123456", ACCESSION, "abc", submissions=submissions, **kwargs
        )
        return record, network

    def test_forward_record_carries_acceptance_fields_and_actual_receipt(self):
        record, network = self.collect({SUBMISSIONS: recent()})
        self.assertEqual(network.calls[0][0], SUBMISSIONS)
        self.assertEqual(record["accepted_at"], record["published_at"])
        self.assertEqual(record["published_at"], "2026-09-25T20:30:00Z")
        self.assertIs(record["after_hours"], False)
        self.assertEqual(record["first_seen_at"], "2026-09-27T04:00:00Z")
        self.assertEqual(record["mode"], "forward")
        self.assertEqual(record["sec_acceptance_raw"], "2026-09-25T16:30:00-04:00")
        self.assertEqual(record["cik"], CIK)
        self.assertEqual(record["symbol"], "ABC")

    def test_supplied_submissions_save_a_request(self):
        record, network = self.collect(submissions=recent())
        self.assertEqual([call[0] for call in network.calls], list(self.DOCS))
        self.assertEqual(record["id"], f"sec:{ACCESSION}:d123ex991.htm")

    def test_after_hours_starts_at_1730_eastern_and_reads_utc_labels_late(self):
        for acceptance, expected in [
            ("2026-09-25T17:29:59-04:00", False),
            ("2026-09-25T17:30:00-04:00", True),
            ("2026-01-06T17:30:00-05:00", True),
            ("2026-09-25T21:29:00Z", True),  # 17:29 EDT, or 21:29 if SEC means Eastern
            ("2026-09-25T13:00:00Z", False),
            ("2026-09-25T18:01:14.000Z", True),
        ]:
            with self.subTest(acceptance=acceptance):
                record, _ = self.collect(submissions=recent(acceptanceDateTime=[acceptance]))
                self.assertIs(record["after_hours"], expected)

    def test_latest_acceptance_never_reads_early(self):
        utc = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(sec.latest_acceptance("2026-09-25T16:30:00-04:00"), utc.replace(hour=20))
        self.assertEqual(sec.latest_acceptance("2026-09-25T16:30:00.000Z"), utc.replace(hour=20))
        self.assertEqual(sec.latest_acceptance("2026-09-25T16:30:00"), utc.replace(hour=20))
        self.assertEqual(sec.latest_acceptance("2026-09-25T16:30:00+01:00"), utc.replace(hour=15))
        # 01:30 happens twice when daylight saving ends; the later (EST) instant wins.
        self.assertEqual(
            sec.latest_acceptance("2026-11-01T01:30:00"),
            datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),
        )
        for value in ["2026-09-25", "", None, "soon"]:
            with self.subTest(value=value), self.assertRaises(sec.SECError):
                sec.latest_acceptance(value)

    def test_non_qualifying_filing_returns_none_without_document_requests(self):
        for changes in [
            {"items": ["2.02,7.01"]},
            {"items": ["9.01"]},
            {"form": ["10-K"]},
            {"primaryDocument": ["cover.pdf"]},
            {"acceptanceDateTime": ["2026-09-25"]},
        ]:
            with self.subTest(changes=changes):
                record, network = self.collect(submissions=recent(**changes))
                self.assertIsNone(record)
                self.assertEqual(network.calls, [])

    def test_missing_accession_raises_filing_not_found(self):
        other = recent(accessionNumber=["0000123456-26-000999"])
        with self.assertRaises(sec.FilingNotFound):
            self.collect(submissions=other)
        self.assertTrue(issubclass(sec.FilingNotFound, sec.SECError))

    def test_submissions_of_another_company_are_refused(self):
        data = recent()
        for reported in ["654321", "abc", 654321]:
            data["cik"] = reported
            with self.subTest(reported=reported), self.assertRaises(sec.SECError):
                self.collect(submissions=data)
        data["cik"] = "123456"
        self.assertIsNotNone(self.collect(submissions=data)[0])
        with self.assertRaises(sec.SECError):
            self.collect(submissions=[])

    def test_first_seen_rules_by_mode(self):
        late = "2026-09-26T10:00:00Z"
        record, _ = self.collect(submissions=recent(), mode="historical", first_seen=late)
        self.assertEqual((record["first_seen_at"], record["mode"]), (late, "historical"))
        seen = []

        def assume(raw):
            seen.append(raw)
            return "2026-09-25T20:45:00+00:00"

        record, _ = self.collect(submissions=recent(), mode="historical", first_seen=assume)
        self.assertEqual(seen, ["2026-09-25T16:30:00-04:00"])
        self.assertEqual(record["first_seen_at"], "2026-09-25T20:45:00Z")
        for kwargs in [
            {"mode": "forward", "first_seen": late},
            {"mode": "historical"},
            {"mode": "synthetic", "first_seen": late},
            {"mode": "historical", "first_seen": "2026-09-25T20:29:59Z"},
            {"mode": "historical", "first_seen": "2026-09-26T10:00:00"},
            {"mode": "historical", "first_seen": "2026-09-26"},
            {"mode": "historical", "first_seen": lambda raw: None},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(sec.SECError):
                self.collect(submissions=recent(), **kwargs)

    def test_utc_labelled_acceptance_needs_first_seen_after_eastern_reading(self):
        data = recent(acceptanceDateTime=["2026-09-25T16:30:00.000Z"])
        with self.assertRaises(sec.SECError):
            self.collect(submissions=data, mode="historical", first_seen="2026-09-25T16:45:00Z")
        record, _ = self.collect(
            submissions=data, mode="historical", first_seen="2026-09-25T20:45:00Z"
        )
        self.assertEqual(record["published_at"], "2026-09-25T16:30:00Z")

    def test_invalid_arguments_never_make_a_request(self):
        network = FakeNetwork({})
        client = network.client("Bot a@b.test", 20, 4)
        for args in [
            ("0", ACCESSION, "ABC"),
            ("12345678901", ACCESSION, "ABC"),
            ("\u0661\u0662", ACCESSION, "ABC"),
            ("123456", "../../x", "ABC"),
            ("123456", ACCESSION.replace("0", "\u0660"), "ABC"),
            ("123456", ACCESSION, "A/B"),
            ("123456", ACCESSION, ""),
        ]:
            with self.subTest(args=args), self.assertRaises(sec.SECError):
                sec.collect_filing(client, *args)
        self.assertEqual(network.calls, [])


class FeedURLTests(unittest.TestCase):
    FEED = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company="
        "&dateb=&owner=include&start=0&count={}&output=atom"
    )

    def test_feed_ticker_and_daily_index_urls_are_approved(self):
        for url in [
            self.FEED.format(100),
            self.FEED.format(10),
            "https://www.sec.gov/files/company_tickers.json",
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260925.idx",
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR1/form.20260331.idx",
            "https://www.sec.gov/Archives/edgar/daily-index/2024/QTR1/form.20240229.idx",
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261231.idx",
        ]:
            with self.subTest(url=url):
                sec._validate_url(url)

    def test_near_miss_feed_ticker_and_index_urls_are_blocked(self):
        feed = self.FEED.format(100)
        for url in [
            self.FEED.format(50),
            self.FEED.format(1000),
            feed.replace("type=8-K", "type=4"),
            feed.replace("action=getcurrent&type=8-K", "type=8-K&action=getcurrent"),
            feed + "&extra=1",
            feed.replace("output=atom", "output=xml"),
            feed.replace("start=0", "start=100"),
            feed.replace("company=", "company=x"),
            feed + "#top",
            feed.replace("https://", "http://"),
            feed.replace("www.sec.gov", "www.sec.gov:443"),
            feed.replace("www.sec.gov", "user@www.sec.gov"),
            feed.replace("/cgi-bin/browse-edgar", "/cgi-bin/browse-edgar2"),
            feed.replace("&count", "\n&count"),
            feed.replace("&count", "\t&count"),
            "https://www.sec.gov/cgi-bin/browse-edgar",
            "https://www.sec.gov/cgi-bin/srch-edgar?text=form-type%3D8-K",
            "https://data.sec.gov/cgi-bin/browse-edgar?action=getcurrent",
            "https://www.sec.gov/files/company_tickers.json?x=1",
            "https://www.sec.gov/files/company_tickers_exchange.json",
            "https://www.sec.gov/files/../files/company_tickers.json",
            "https://sec.gov/files/company_tickers.json",
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR2/form.20260925.idx",
            "https://www.sec.gov/Archives/edgar/daily-index/2025/QTR3/form.20260925.idx",
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR1/form.20260230.idx",
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/master.20260925.idx",
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260925.idx.gz",
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/",
            "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260925.idx?x",
            "https://www.sec.gov/Archives/edgar/data/123456/000012345626000001/ex99.htm?",
            BASE + "d\u00e9x99.htm",
        ]:
            with self.subTest(url=url), self.assertRaises(sec.SECError):
                sec._validate_url(url)

    def test_bulk_reads_have_their_own_byte_bound(self):
        url = "https://www.sec.gov/files/company_tickers.json"
        network = FakeNetwork({url: b"x" * (sec.MAX_RESPONSE_BYTES + 1)})
        client = network.client("Bot a@b.test", 20, 3)
        with self.assertRaises(sec.SECError):
            client.get(url)
        self.assertEqual(len(client.get(url, max_bytes=sec.MAX_BULK_BYTES)), 2_000_001)
        network.responses[url] = b"x" * (sec.MAX_BULK_BYTES + 1)
        with self.assertRaises(sec.SECError):
            client.get(url, max_bytes=sec.MAX_BULK_BYTES)


if __name__ == "__main__":
    unittest.main()
