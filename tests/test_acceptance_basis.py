"""A verified acceptance instant from the filing's -index.htm 'Accepted' value (#16).

The index page layout below mirrors EDGAR's filing index ("infoHead"/"info" pairs); the
timestamps come from the real pairs in tests/fixtures/edgar_acceptance.json (#15).
"""

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jevtrader import sec
from jevtrader.common import EASTERN

FIXTURE = Path(__file__).parent / "fixtures" / "edgar_acceptance.json"
FILINGS = json.loads(FIXTURE.read_text(encoding="utf-8"))["filings"]
IDS = [row["accession"] for row in FILINGS]

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def index_page(accepted: str, *, extra: str = "") -> str:
    return (
        '<html><body><div class="formGrouping">'
        '<div class="infoHead">Filing Date</div><div class="info">2026-07-30</div>'
        f'<div class="infoHead">Accepted</div><div class="info">{accepted}</div>{extra}'
        '<div class="infoHead">Documents</div><div class="info">3</div>'
        "</div></body></html>"
    )


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str):
        super().__init__(body)
        self.url = url

    def geturl(self) -> str:
        return self.url


class Network:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def transport(self, request, *, timeout):
        url = request.full_url
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, dict):
            value = json.dumps(value)
        return Response(value.encode(), url)


def collect(cik: int, accession: str, raw: str, accepted: str | None, **kwargs):
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"
    responses = {
        base + "index.json": {"directory": {"item": [{"name": "ex99.htm"}]}},
        base + "ex99.htm": "<p>Operating update.</p>",
    }
    if accepted is not None:
        responses[sec.index_url(str(cik), accession)] = index_page(accepted)
    network = Network(responses)
    client = sec._SECClient(
        "Research contact@research.test",
        20,
        5,
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


def true_instant(row: dict) -> datetime:
    wall = datetime.strptime(row["index_accepted_et"], "%Y-%m-%d %H:%M:%S")
    return wall.replace(tzinfo=EASTERN).astimezone(timezone.utc)


def z(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_index_url_is_the_filing_index_page_and_already_allowlisted():
    url = sec.index_url("320193", "0000320193-26-000018")
    assert url == FILINGS[0]["index_url"]
    sec.validate_url(url)


@pytest.mark.parametrize("row", FILINGS, ids=IDS)
def test_verified_record_uses_the_index_accepted_instant(row):
    record, network = collect(
        row["cik"],
        row["accession"],
        row["json_acceptance"],
        row["index_accepted_et"],
        verify_acceptance=True,
    )
    truth = true_instant(row)
    assert row["index_url"] in network.calls
    assert record["acceptance_basis"] == "edgar_index_accepted"
    assert record["published_at"] == z(truth)
    assert record["accepted_at"] == z(truth)
    assert record["after_hours"] is (truth.astimezone(EASTERN).time() >= sec.AFTER_HOURS)
    assert record["sec_acceptance_raw"] == row["json_acceptance"]


def test_apple_1630_filing_is_not_after_hours_once_verified():
    row = FILINGS[0]
    assert row["index_accepted_et"].endswith("16:30:28")
    unverified, _ = collect(row["cik"], row["accession"], row["json_acceptance"], None)
    assert unverified["after_hours"] is True  # the latest reading, 20:30 ET
    verified, _ = collect(
        row["cik"],
        row["accession"],
        row["json_acceptance"],
        "2026-07-30 16:30:28",
        verify_acceptance=True,
    )
    assert verified["after_hours"] is False


def test_eastern_z_filer_is_no_longer_published_five_hours_early():
    row = next(r for r in FILINGS if r["accession"] == "0001137411-18-000111")
    unverified, _ = collect(row["cik"], row["accession"], row["json_acceptance"], None)
    verified, _ = collect(
        row["cik"],
        row["accession"],
        row["json_acceptance"],
        row["index_accepted_et"],
        verify_acceptance=True,
    )
    assert unverified["published_at"] == "2018-11-26T16:09:52Z"
    assert verified["published_at"] == "2018-11-26T21:09:52Z"


def test_default_collection_is_unverified_and_makes_no_index_request():
    row = FILINGS[0]
    record, network = collect(row["cik"], row["accession"], row["json_acceptance"], None)
    assert record["acceptance_basis"] == "submissions_json_unverified"
    assert all(not url.endswith("-index.htm") for url in network.calls)


@pytest.mark.parametrize(
    ("accepted", "expected"),
    [("2026-09-25 17:29:59", False), ("2026-09-25 17:30:00", True)],
)
def test_after_hours_boundary_on_the_verified_instant(accepted, expected):
    raw = accepted.replace(" ", "T") + ".000Z"  # an Eastern-wall-clock "Z"
    record, _ = collect(123456, "0000123456-26-000001", raw, accepted, verify_acceptance=True)
    assert record["after_hours"] is expected


def test_index_value_that_matches_neither_json_reading_fails_closed():
    with pytest.raises(sec.SECError):
        collect(
            320193,
            "0000320193-26-000018",
            "2026-07-30T20:30:28.000Z",
            "2026-07-30 16:31:28",
            verify_acceptance=True,
        )


@pytest.mark.parametrize(
    "page",
    [
        "<html><p>No accepted value</p></html>",
        index_page("2026-07-30"),
        index_page("2026-07-30 16:30:28", extra=index_page("2026-07-30 16:30:28")),
        index_page("2026-13-30 16:30:28"),
    ],
    ids=["missing", "date-only", "twice", "bad-month"],
)
def test_missing_ambiguous_or_malformed_accepted_fails_closed(page):
    with pytest.raises(sec.SECError):
        sec.verified_acceptance(page.encode(), "2026-07-30T20:30:28.000Z")


def test_ambiguous_fall_back_wall_time_reads_the_later_instant():
    # 01:30 happens twice on 2026-11-01; the EST (later) instant is never early.
    verified = sec.verified_acceptance(
        index_page("2026-11-01 01:30:00").encode(), "2026-11-01T01:30:00.000Z"
    )
    assert verified == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)


def test_verified_instant_never_exceeds_the_conservative_reading():
    for row in FILINGS:
        verified = sec.verified_acceptance(
            index_page(row["index_accepted_et"]).encode(), row["json_acceptance"]
        )
        assert verified == true_instant(row)
        assert verified <= sec.latest_acceptance(row["json_acceptance"])


def test_historical_first_seen_rule_is_unchanged_by_verification():
    row = FILINGS[0]
    with pytest.raises(sec.SECError):
        collect(
            row["cik"],
            row["accession"],
            row["json_acceptance"],
            row["index_accepted_et"],
            verify_acceptance=True,
            mode="historical",
            first_seen="2026-07-30T20:45:28Z",  # before the latest reading, 00:30:28Z next day
        )
