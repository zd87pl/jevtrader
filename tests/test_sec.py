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


if __name__ == "__main__":
    unittest.main()
