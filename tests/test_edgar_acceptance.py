"""Real EDGAR acceptance pairs (index 'Accepted' vs submissions JSON) against sec's readings."""

import json
import re
from datetime import datetime, time, timezone
from pathlib import Path

import pytest

from jevtrader import sec
from jevtrader.common import EASTERN

FIXTURE = Path(__file__).parent / "fixtures" / "edgar_acceptance.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
FILINGS = DATA["filings"]
IDS = [row["accession"] for row in FILINGS]
FIELDS = {
    "cik",
    "company",
    "accession",
    "form",
    "index_url",
    "submissions_url",
    "index_accepted_et",
    "json_acceptance",
    "z_meaning",
    "json_source",
    "confidence",
}


def true_instant(row: dict) -> datetime:
    """The index page's Eastern wall-clock 'Accepted', as a UTC instant."""
    wall = datetime.strptime(row["index_accepted_et"], "%Y-%m-%d %H:%M:%S")
    return wall.replace(tzinfo=EASTERN).astimezone(timezone.utc)


def test_provenance_and_capture_date_are_recorded():
    assert DATA["provenance"] == "read via WebFetch by an agent; owner browser spot-check pending"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", DATA["captured"])
    assert DATA["owner_check"]


@pytest.mark.parametrize("row", FILINGS, ids=IDS)
def test_each_row_is_complete_and_urls_name_its_accession(row):
    assert set(row) == FIELDS
    cik, accession = row["cik"], row["accession"]
    assert re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)
    folder = accession.replace("-", "")
    assert row["index_url"] == (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}-index.htm"
    )
    assert row["submissions_url"] == f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    assert row["z_meaning"] in {"utc", "eastern_wall_clock"}
    assert row["json_acceptance"].endswith("Z")


def test_fixture_covers_the_required_populations():
    assert len(FILINGS) >= 4
    assert len(set(IDS)) == len(IDS)
    assert any(true_instant(row).year < 2020 for row in FILINGS)
    assert LATE and sec.AFTER_HOURS == time(17, 30)
    assert {row["z_meaning"] for row in FILINGS} == {"utc", "eastern_wall_clock"}


@pytest.mark.parametrize("row", FILINGS, ids=IDS)
def test_z_meaning_matches_the_index_page(row):
    z_reading = datetime.fromisoformat(row["json_acceptance"].replace("Z", "+00:00"))
    if row["z_meaning"] == "utc":
        assert z_reading == true_instant(row)
    else:
        assert z_reading.strftime("%Y-%m-%d %H:%M:%S") == row["index_accepted_et"]
        assert z_reading != true_instant(row)


@pytest.mark.parametrize("row", FILINGS, ids=IDS)
def test_latest_acceptance_never_backdates_a_real_filing(row):
    assert sec.latest_acceptance(row["json_acceptance"]) >= true_instant(row)


def after_1730(row: dict) -> bool:
    return true_instant(row).astimezone(EASTERN).time() >= sec.AFTER_HOURS


LATE = [row for row in FILINGS if after_1730(row)]


@pytest.mark.parametrize("row", LATE, ids=[row["accession"] for row in LATE])
def test_a_real_after_1730_filing_reads_after_hours(row):
    latest = sec.latest_acceptance(row["json_acceptance"]).astimezone(EASTERN)
    assert latest.time() >= sec.AFTER_HOURS
