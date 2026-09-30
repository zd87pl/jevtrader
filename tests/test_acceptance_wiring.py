"""The verified acceptance instant is wired into every collection path (#16).

collect_filing(verify_acceptance=True, unverified_fallback=True) reads the -index.htm
'Accepted' instant; when that page is unavailable it keeps the later of the two JSON
readings, marked unverified. No path makes a filing visible earlier than before.
"""

import json
from datetime import date, datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest
from test_acceptance_basis import FILINGS, IDS, NOW, Response, index_page
from test_feeds import (
    ABC,
    FEED,
    TICKER_JSON,
    TICKERS,
    UA,
    FeedsCase,
    Filing,
    accession,
    atom,
    entry,
    form_index,
    http_error,
    submissions_url,
)
from test_feeds import index_url as daily_index_url

from jevtrader import feeds, sec
from jevtrader.store import Ledger

FALLBACK = "submissions_json_latest_unverified"


class Network:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def transport(self, request, *, timeout):
        url = request.full_url
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, dict):
            value = json.dumps(value)
        return Response(value.encode(), url)


def collect(raw: str, index: object, *, requests: int = 5, **kwargs):
    cik, accession = 123456, "0000123456-26-000001"
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"
    responses: dict = {
        base + "index.json": {"directory": {"item": [{"name": "ex99.htm"}]}},
        base + "ex99.htm": "<p>Operating update.</p>",
    }
    if index is not None:
        responses[sec.index_url(str(cik), accession)] = index
    network = Network(responses)
    client = sec._SECClient(
        "Research contact@research.test",
        20,
        requests,
        transport=network.transport,
        clock=lambda: 0.0,
        sleep=lambda _: None,
        now=lambda: NOW,
    )
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "form": ["8-K"],
                "primaryDocument": ["cover.htm"],
                "acceptanceDateTime": [raw],
                "items": ["7.01"],
            }
        }
    }
    record = sec.collect_filing(
        client, str(cik), accession, "abc", submissions=submissions, **kwargs
    )
    return record, network


def missing(code: int = 404) -> HTTPError:
    return HTTPError("https://www.sec.gov/x", code, "error", {}, None)


RAW = "2026-07-30T17:45:00.000Z"  # 13:45 ET read as UTC, 17:45 ET read as Eastern
LATEST = "2026-07-30T21:45:00Z"


@pytest.mark.parametrize(
    "index",
    [missing(404), missing(410), "<html><p>No accepted value</p></html>"],
    ids=["404", "410", "no-accepted"],
)
def test_unavailable_index_falls_back_to_the_later_reading(index):
    record, _ = collect(RAW, index, verify_acceptance=True, unverified_fallback=True)
    assert record["acceptance_basis"] == FALLBACK
    assert record["published_at"] == LATEST
    assert record["accepted_at"] == LATEST
    assert record["after_hours"] is True  # 17:45 ET, the later reading


def test_exhausted_budget_falls_back_without_a_request():
    record, network = collect(
        RAW,
        index_page("2026-07-30 17:45:00"),
        requests=2,
        verify_acceptance=True,
        unverified_fallback=True,
    )
    assert record["acceptance_basis"] == FALLBACK
    assert all(not url.endswith("-index.htm") for url in network.calls)


def test_verified_index_still_wins_with_a_fallback_allowed():
    record, _ = collect(
        RAW, index_page("2026-07-30 17:45:00"), verify_acceptance=True, unverified_fallback=True
    )
    assert record["acceptance_basis"] == "edgar_index_accepted"
    assert record["published_at"] == LATEST


def test_disagreeing_index_still_fails_closed_with_a_fallback_allowed():
    with pytest.raises(sec.AcceptanceMismatch):
        collect(
            RAW,
            index_page("2026-07-30 17:46:00"),
            verify_acceptance=True,
            unverified_fallback=True,
        )


@pytest.mark.parametrize(
    "error",
    [missing(403), missing(429), missing(503), URLError("down")],
    ids=["403", "429", "503", "network"],
)
def test_throttling_or_outage_on_the_index_propagates(error):
    # So the batch stops and the filing is retried, rather than recorded unverified.
    with pytest.raises((HTTPError, URLError)):
        collect(RAW, error, verify_acceptance=True, unverified_fallback=True)


def test_without_a_fallback_an_unavailable_index_still_raises():
    with pytest.raises(HTTPError):
        collect(RAW, missing(404), verify_acceptance=True)


def test_fallback_requires_verification():
    with pytest.raises(sec.SECError):
        collect(RAW, None, unverified_fallback=True)


@pytest.mark.parametrize("row", FILINGS, ids=IDS)
def test_no_path_publishes_earlier_than_the_unverified_default(row):
    raw = row["json_acceptance"]
    before, _ = collect(raw, None)
    verified, _ = collect(
        raw,
        index_page(row["index_accepted_et"]),
        verify_acceptance=True,
        unverified_fallback=True,
    )
    fallback, _ = collect(raw, missing(), verify_acceptance=True, unverified_fallback=True)
    for record in (verified, fallback):
        assert record["published_at"] >= before["published_at"]
        assert record["first_seen_at"] == before["first_seen_at"]
    assert fallback["published_at"] >= verified["published_at"]


def test_fallback_basis_is_a_known_acceptance_basis():
    assert FALLBACK in sec.ACCEPTANCE_BASES


# The collection paths: poll, reconcile, backfill and collect_disclosures.


# The Filing default acceptance: 16:30:00 ET with an explicit offset, so both readings agree.
ACCEPTED_ET = "2026-09-25 16:30:00"
ACCEPTED_Z = "2026-09-25T20:30:00Z"
STORED_Z = "2026-09-25T20:30:00.000000Z"  # the ledger's canonical instant form


def filing_index(filing) -> str:
    return sec.index_url(filing.cik, filing.accession)


class WiredCollectionTests(FeedsCase):
    def setUp(self):
        super().setUp()
        self.memory = feeds.PollMemory(clock=lambda: 0.0)
        self.net.responses[TICKERS] = TICKER_JSON

    def poll(self, filing, index):
        self.net.add(filing)
        self.net.responses[filing_index(filing)] = index
        self.net.responses[FEED] = atom(entry(filing.cik, filing.accession))
        return feeds.poll(
            self.ledger,
            UA,
            symbols=None,
            transport=self.net.transport,
            memory=self.memory,
            verify_acceptance=True,
        )

    def test_poll_records_the_verified_index_instant(self):
        filing = Filing(ABC, accession(1), acceptance="2026-09-25T16:30:00.000Z")
        result = self.poll(filing, index_page(ACCEPTED_ET))
        self.assertEqual(result["added"], [filing.id])
        record = self.ledger.get("disclosures", filing.id)
        self.assertEqual(record["acceptance_basis"], "edgar_index_accepted")
        # The JSON "Z" read as UTC would have said 16:30Z, four hours early.
        self.assertEqual(record["published_at"], STORED_Z)
        self.assertEqual(record["accepted_at"], ACCEPTED_Z)
        self.assertFalse(record["after_hours"])
        self.assertEqual(self.net.urls()[-1], filing_index(filing))
        self.assertEqual(result["requests"], 6)

    def test_poll_falls_back_to_the_later_reading_when_the_index_is_gone(self):
        filing = Filing(ABC, accession(1), acceptance="2026-09-25T16:30:00.000Z")
        result = self.poll(filing, http_error(filing_index(filing), 404))
        self.assertEqual((result["added"], result["errors"]), ([filing.id], []))
        record = self.ledger.get("disclosures", filing.id)
        self.assertEqual(record["acceptance_basis"], "submissions_json_latest_unverified")
        self.assertEqual(record["published_at"], STORED_Z)

    def test_poll_stops_on_a_throttled_index_and_records_nothing(self):
        filing = Filing(ABC, accession(1))
        result = self.poll(filing, http_error(filing_index(filing), 429))
        self.assertEqual(result["added"], [])
        self.assertIsNotNone(result["stopped"])
        self.assertIsNone(self.ledger.get("disclosures", filing.id))

    def test_poll_budget_covers_the_index_request(self):
        self.assertEqual(feeds.REQUESTS_PER_FILING + feeds.VERIFY_REQUESTS, 5)

    def test_verify_acceptance_must_be_a_boolean(self):
        with self.assertRaises(ValueError):
            feeds.poll(self.ledger, UA, symbols=None, verify_acceptance="yes")

    def test_poll_without_verification_fetches_no_index(self):
        filing = Filing(ABC, accession(1))
        self.net.add(filing)
        self.net.responses[FEED] = atom(entry(filing.cik, filing.accession))
        result = feeds.poll(
            self.ledger, UA, symbols=None, transport=self.net.transport, memory=self.memory
        )
        self.assertEqual(result["added"], [filing.id])
        self.assertNotIn(filing_index(filing), self.net.urls())
        record = self.ledger.get("disclosures", filing.id)
        self.assertEqual(record["acceptance_basis"], "submissions_json_unverified")

    def test_reconcile_records_the_verified_index_instant(self):
        day = date(2026, 9, 25)
        filing = Filing(ABC, accession(1))
        self.net.add(filing)
        self.net.responses[filing_index(filing)] = index_page(ACCEPTED_ET)
        self.net.responses[daily_index_url(day)] = form_index(
            ("8-K", "Company", ABC, "2026-09-25", filing.accession)
        )
        with patch.object(
            sec, "utc_now", lambda: datetime(2026, 9, 26, 2, 45, tzinfo=timezone.utc)
        ):
            result = feeds.reconcile(
                self.ledger,
                UA,
                day,
                symbols=None,
                transport=self.net.transport,
                memory=self.memory,
                verify_acceptance=True,
            )
        self.assertEqual(result["recovered"], [filing.id])
        record = self.ledger.get("disclosures", filing.id)
        self.assertEqual(record["acceptance_basis"], "edgar_index_accepted")
        self.assertEqual(record["published_at"], STORED_Z)

    def test_backfill_records_the_verified_instant_and_keeps_the_assumed_first_seen(self):
        research = Ledger(":memory:")
        self.addCleanup(research.db.close)
        day = date(2026, 9, 18)
        filing = Filing(ABC, accession(1), acceptance="2026-09-18T18:05:00.000Z")
        self.net.add(filing)
        self.net.responses[filing_index(filing)] = index_page("2026-09-18 18:05:00")
        self.net.responses[daily_index_url(day)] = form_index(
            ("8-K", "Company", ABC, "20260918", filing.accession)
        )
        result = feeds.backfill(
            research, UA, day, day, transport=self.net.transport, verify_acceptance=True
        )
        self.assertEqual(result["added"], [filing.id])
        record = research.get("disclosures", filing.id)
        self.assertEqual(record["acceptance_basis"], "edgar_index_accepted")
        self.assertEqual(record["published_at"], "2026-09-18T22:05:00.000000Z")
        self.assertTrue(record["after_hours"])
        self.assertEqual(
            datetime.fromisoformat(record["first_seen_at"]),
            datetime.fromisoformat(feeds.assumed_first_seen("2026-09-18T18:05:00.000Z")),
        )
        self.assertEqual(self.net.urls().count(submissions_url(ABC)), 1)

    def test_collect_disclosures_records_the_verified_instant(self):
        filing = Filing(ABC, accession(1), acceptance="2026-09-25T16:30:00.000Z")
        self.net.add(filing)
        self.net.responses[filing_index(filing)] = index_page(ACCEPTED_ET)
        (record,) = sec.collect_disclosures(
            ABC,
            "ABC",
            user_agent=UA,
            limit=1,
            transport=self.net.transport,
            verify_acceptance=True,
        )
        self.assertEqual(record["acceptance_basis"], "edgar_index_accepted")
        self.assertEqual(record["published_at"], ACCEPTED_Z)

    def test_collect_disclosures_without_verification_keeps_the_old_budget(self):
        filing = Filing(ABC, accession(1), acceptance="2026-09-25T16:30:00.000Z")
        self.net.add(filing)
        (record,) = sec.collect_disclosures(
            ABC, "ABC", user_agent=UA, limit=1, transport=self.net.transport
        )
        self.assertEqual(record["acceptance_basis"], "submissions_json_unverified")
        self.assertNotIn(filing_index(filing), self.net.urls())


def test_collect_command_asks_for_the_verified_instant(tmp_path, monkeypatch):
    from jevtrader.cli import main

    monkeypatch.setenv("SEC_USER_AGENT", "lab alias@example.test")
    db = tmp_path / "forward.sqlite"
    Ledger(db).close()
    with patch("jevtrader.cli.collect_disclosures", return_value=[]) as collect:
        code = main(["--db", str(db), "collect", "--cik", "1", "--symbol", "ABC"])
    assert code == 0
    assert collect.call_args.kwargs["verify_acceptance"] is True


def test_daemon_asks_the_real_collectors_for_the_verified_instant():
    from jevtrader import daemon

    assert daemon._verified(feeds.poll, feeds.poll) == {"verify_acceptance": True}
    assert daemon._verified(feeds.reconcile, feeds.reconcile) == {"verify_acceptance": True}
    assert daemon._verified(lambda *a, **k: {}, feeds.poll) == {}


def test_app_backfill_asks_for_the_verified_instant(tmp_path):
    from jevtrader import app, config, paths

    Ledger(paths.research_ledger_path()).close()
    settings = config.validate({"sec_user_agent": UA, "universe": "all"})
    with patch.object(app.feeds, "backfill", return_value={"added": []}) as backfill:
        app.backfill(paths.research_ledger_path(), settings, date(2026, 1, 5), date(2026, 1, 6))
    assert backfill.call_args.kwargs["verify_acceptance"] is True
