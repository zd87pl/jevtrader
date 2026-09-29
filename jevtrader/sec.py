"""Bounded, read-only SEC collector for forward operating-disclosure research.

Only 8-K/8-K/A filings explicitly listing 7.01 or 8.01 and not 2.02 qualify.
Missing item metadata is skipped. Item numbers are an imperfect non-earnings
filter: 7.01/8.01 can still contain earnings-related material, and other operating
disclosures will be missed. A directory index does not expose exhibit types;
EX-99 selection therefore uses a filename heuristic or a cover-page link label.
One exhibit per filing is collected, not every attachment. A primary-document
fallback is explicitly marked and may be only a cover page.

Acceptance is not public availability. ``first_seen_at`` is when this collector
actually received the selected document, never a reconstructed historical date.
Consumers must persist that first observation rather than overwrite it on repeat
polls, deduplicate on ``id``, and never backdate a forward decision. Amendments are
separate records. No API key, external link fetching, or live model is used.
Only a historical collection may carry a supplied (assumed) ``first_seen_at``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import EASTERN
from .ratelimit import SharedLimiter

MAX_RESPONSE_BYTES = 2_000_000
MAX_BULK_BYTES = 10_000_000  # One day's form index or the ticker map; both are single SEC files.
MAX_TEXT_CHARS = 200_000
MAX_DIRECTORY_FILES = 1_000
MAX_RECENT_FILINGS = 1_000
MAX_LIMIT = 20
REQUEST_INTERVAL = 0.21  # Safely below SEC's 10/s ceiling; at most 5/s.
_DOCUMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.(?:htm|html|txt)\Z", re.I)
_EXHIBIT = re.compile(r"(?:ex(?:hibit|h)?)[_.-]*99", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}", re.A)
_SYMBOL = re.compile(r"[A-Za-z][A-Za-z0-9.-]{0,14}")
AFTER_HOURS = clock_time(17, 30)  # EDGAR assigns the next business day's filing date from here.
FORMS = ("8-K", "8-K/A")
MODES = ("forward", "historical")
# The only approved query: the newest 8-K filings as Atom, with a fixed parameter order.
_CURRENT_8K_QUERY = re.compile(
    r"action=getcurrent&type=8-K&company=&dateb=&owner=include&start=0"
    r"&count=(?:10|20|40|80|100)&output=atom"
)
_DAILY_INDEX = re.compile(
    r"/Archives/edgar/daily-index/(\d{4})/QTR([1-4])/form\.(\d{4})(\d{2})(\d{2})\.idx"
)


class SECError(ValueError):
    """Unsafe input, malformed SEC response, or an exceeded collection bound."""


class FilingNotFound(SECError):
    """The accession is absent from the company's recent submissions (not yet indexed or older)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise SECError("Observation clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _published_at(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SECError("Missing acceptanceDateTime")
    # A date-only value cannot establish when the disclosure was accepted.
    if "T" not in value and " " not in value:
        raise SECError("acceptanceDateTime must include time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SECError("Invalid acceptanceDateTime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return _iso_utc(parsed)


def latest_acceptance(value: str) -> datetime:
    """The latest instant an SEC acceptanceDateTime can denote.

    The submissions API appears to label Eastern wall-clock time with "Z" (unverified).
    Keeping the later of both readings can only delay availability, never backdate it.
    """
    _published_at(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None and parsed.utcoffset() != timedelta(0):
        return parsed.astimezone(timezone.utc)
    # Both folds, so an ambiguous wall time at the end of daylight saving reads late too.
    readings = [
        parsed.replace(tzinfo=EASTERN, fold=fold).astimezone(timezone.utc) for fold in (0, 1)
    ]
    if parsed.tzinfo is not None:
        readings.append(parsed.astimezone(timezone.utc))
    return max(readings)


_ACCEPTED = re.compile(r"\bAccepted\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\b")
ACCEPTANCE_BASES = ("edgar_index_accepted", "submissions_json_unverified")


def index_url(cik: str, accession: str) -> str:
    """The filing's EDGAR index page, whose 'Accepted' value is Eastern wall-clock time."""
    cik = _check_cik(cik)
    if not isinstance(accession, str) or not _ACCESSION.fullmatch(accession):
        raise SECError("Accession must look like 0000000000-00-000000")
    folder = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{folder}/{accession}-index.htm"


def _eastern_wall(wall: datetime) -> datetime:
    """A naive Eastern wall time as UTC; an ambiguous fall-back time reads at its later instant."""
    return max(wall.replace(tzinfo=EASTERN, fold=fold).astimezone(timezone.utc) for fold in (0, 1))


def verified_acceptance(index_page: bytes, raw: str) -> datetime:
    """The acceptance instant from an -index.htm 'Accepted' value, checked against the JSON.

    The index value must appear exactly once and equal one reading of the submissions
    JSON acceptanceDateTime (its "Z" as UTC, or as Eastern wall clock); anything else
    fails closed. The result never exceeds ``latest_acceptance(raw)``.
    """
    text, _ = _read_document(index_page, "index.htm")
    found = _ACCEPTED.findall(text)
    if len(found) != 1:
        raise SECError("Filing index must show exactly one Accepted timestamp")
    try:
        wall = datetime.strptime(found[0], "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise SECError("Invalid Accepted timestamp on the filing index") from exc
    instant = _eastern_wall(wall)
    _published_at(raw)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    readings = {latest_acceptance(raw)}
    if parsed.tzinfo is not None:
        readings.add(parsed.astimezone(timezone.utc).replace(microsecond=0))
    if instant not in {reading.replace(microsecond=0) for reading in readings}:
        raise SECError("Filing index Accepted disagrees with SEC acceptanceDateTime")
    return instant


def _daily_index_path(match: re.Match[str]) -> bool:
    year, quarter, *day = (int(part) for part in match.groups())
    try:
        parsed = date(*day)
    except ValueError:
        return False
    return parsed.year == year and (parsed.month - 1) // 3 + 1 == quarter


def validate_url(url: str) -> None:
    # urlsplit silently drops tabs and newlines, so reject every non-printable byte first.
    if not isinstance(url, str) or any(not 33 <= ord(char) <= 126 for char in url):
        raise SECError("SEC requests require an approved HTTPS URL")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.port:
        raise SECError("SEC requests require an approved HTTPS URL")
    if "#" in url:
        raise SECError("SEC request URLs cannot contain queries or fragments")
    if parts.netloc == "www.sec.gov" and parts.path == "/cgi-bin/browse-edgar":
        if _CURRENT_8K_QUERY.fullmatch(parts.query):
            return
        raise SECError("Only the current 8-K Atom feed query is approved")
    if "?" in url:
        raise SECError("SEC request URLs cannot contain queries or fragments")
    if parts.netloc == "data.sec.gov" and re.fullmatch(r"/submissions/CIK\d{10}\.json", parts.path):
        return
    if parts.netloc == "www.sec.gov":
        if parts.path == "/files/company_tickers.json":
            return
        daily = _DAILY_INDEX.fullmatch(parts.path)
        if daily and _daily_index_path(daily):
            return
        match = re.fullmatch(r"/Archives/edgar/data/\d{1,10}/\d{18}/([^/]+)", parts.path)
        if match and (match[1] == "index.json" or _DOCUMENT.fullmatch(match[1])):
            return
    raise SECError("URL is outside approved SEC submissions/archive/index paths")


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SECError("SEC redirects are not followed")


def urlopen(request: Request, *, timeout: float):
    return build_opener(_NoRedirects()).open(request, timeout=timeout)


class _RateLimiter:
    def __init__(self, clock: Callable, sleep: Callable):
        self.clock = clock
        self.sleep = sleep
        self.last_request: float | None = None
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            if self.last_request is not None:
                delay = REQUEST_INTERVAL - (self.clock() - self.last_request)
                if delay > 0:
                    self.sleep(delay)
            self.last_request = self.clock()


# Shared by ordinary calls in every thread and every process using this app directory
# (daemon, backfill and collect together), so SEC sees at most five requests a second.
_DEFAULT_LIMITER: _RateLimiter | SharedLimiter = SharedLimiter("sec", REQUEST_INTERVAL)


class _SECClient:
    """A synchronous client; injectable dependencies keep tests offline."""

    def __init__(
        self,
        user_agent: str,
        timeout: float,
        max_requests: int,
        *,
        transport: Callable | None = None,
        clock: Callable | None = None,
        sleep: Callable | None = None,
        now: Callable | None = None,
    ):
        if (
            not isinstance(user_agent, str)
            or len(user_agent) > 250
            or "\r" in user_agent
            or "\n" in user_agent
            or not _EMAIL.search(user_agent)
        ):
            raise SECError(
                "Declare a contact for SEC with an email address (an alias is recommended)"
            )
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise SECError("timeout must be between 0 and 60 seconds")
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_requests = max_requests
        self.transport = transport or urlopen
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.now = now or utc_now
        self.requests = 0
        self.limiter: _RateLimiter | SharedLimiter = (
            _DEFAULT_LIMITER
            if clock is None and sleep is None
            else _RateLimiter(self.clock, self.sleep)
        )

    def get(self, url: str, *, max_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
        validate_url(url)
        if self.requests >= self.max_requests:
            raise SECError("SEC request budget exhausted")
        self.limiter.acquire()
        self.requests += 1
        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json,application/atom+xml,text/html,text/plain",
            },
        )
        with self.transport(request, timeout=self.timeout) as response:
            validate_url(response.geturl())
            payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise SECError("SEC response exceeds maximum byte size")
        return payload

    def get_json(self, url: str, *, max_bytes: int = MAX_RESPONSE_BYTES) -> dict:
        try:
            value = json.loads(self.get(url, max_bytes=max_bytes))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SECError("Invalid SEC JSON response") from exc
        if not isinstance(value, dict):
            raise SECError("SEC JSON response must be an object")
        return value


class _DocumentParser(HTMLParser):
    _BLOCKS = {
        "p",
        "div",
        "li",
        "tr",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "section",
    }
    _HIDDEN = {"script", "style", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.hidden: list[str] = []
        self.anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self._HIDDEN:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag in self._BLOCKS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")
        if tag == "a":
            self.anchor = (dict(attrs).get("href", ""), [])

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag in self._BLOCKS:
            self.parts.append("\n")
        if tag == "a" and self.anchor is not None:
            href, text = self.anchor
            if href:
                self.links.append((href, "".join(text)))
            self.anchor = None

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.anchor is not None:
                self.anchor[1].append(data)

    @property
    def text(self) -> str:
        lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        return "\n\n".join(line for line in lines if line)


def _read_document(payload: bytes, filename: str) -> tuple[str, list[tuple[str, str]]]:
    content = payload.decode("utf-8-sig", errors="replace")
    if filename.lower().endswith(".txt") and not re.search(
        r"<(?:html|body|div|p)\b", content, re.I
    ):
        lines = [re.sub(r"[\t ]+", " ", line).strip() for line in unescape(content).splitlines()]
        return "\n\n".join(line for line in lines if line), []
    parser = _DocumentParser()
    parser.feed(content)
    parser.close()
    return parser.text, parser.links


def _directory_names(client: _SECClient, base: str) -> list[str]:
    try:
        listing = client.get_json(base + "index.json")
    except HTTPError as exc:
        if exc.code == 404:
            exc.close()
            return []
        raise
    directory = listing.get("directory")
    entries = directory.get("item") if isinstance(directory, dict) else None
    if not isinstance(entries, list) or len(entries) > MAX_DIRECTORY_FILES:
        raise SECError("Malformed or oversized SEC directory index")
    return [
        entry["name"]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and _DOCUMENT.fullmatch(entry["name"])
    ]


def _exhibit_rank(name: str) -> tuple[int, int, str]:
    normalized = re.sub(r"[_.-]", "", name.lower())
    return (
        0 if re.search(r"ex(?:hibit|h)?991", normalized) else 1,
        1 if name.lower().endswith(".txt") else 0,
        name.lower(),
    )


def _cover_exhibit(links: list[tuple[str, str]], base: str, primary: str) -> str | None:
    candidates = []
    for href, label in links:
        if not (
            _EXHIBIT.search(href)
            or re.search(r"\b(?:ex(?:hibit)?\.?\s*)?99(?:[.\s_-]\d+)?\b", label, re.I)
        ):
            continue
        joined = urlsplit(urljoin(base + primary, href))
        url = urlunsplit((joined.scheme, joined.netloc, joined.path, joined.query, ""))
        if not url.startswith(base):
            continue
        name = url[len(base) :]
        if name == primary or not _DOCUMENT.fullmatch(name):
            continue
        try:
            validate_url(url)
        except (SECError, ValueError):
            continue
        candidates.append(name)
    return min(candidates, key=_exhibit_rank) if candidates else None


def _filing_document(client: _SECClient, base: str, primary: str) -> tuple[str, str, str, str]:
    names = _directory_names(client, base)
    exhibits = [name for name in names if name != primary and _EXHIBIT.search(name)]
    if exhibits:
        name = min(exhibits, key=_exhibit_rank)
        text, _ = _read_document(client.get(base + name), name)
        return name, text, "exhibit", "ex99_filename_heuristic"
    cover_payload = client.get(base + primary)
    cover_text, links = _read_document(cover_payload, primary)
    exhibit = _cover_exhibit(links, base, primary)
    if exhibit:
        text, _ = _read_document(client.get(base + exhibit), exhibit)
        return exhibit, text, "exhibit", "cover_link"
    return primary, cover_text, "primary_document_fallback", "no_supported_ex99_found"


def _check_cik(cik: object) -> str:
    if not isinstance(cik, str) or not re.fullmatch(r"\d{1,10}", cik, re.A) or int(cik) == 0:
        raise SECError("CIK must contain 1 to 10 digits and be nonzero")
    return cik


def _check_symbol(symbol: object) -> str:
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise SECError("Invalid stock symbol")
    return symbol


def _recent_filings(data: dict) -> dict:
    filings = data.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise SECError("Missing SEC recent filings")
    required = ("accessionNumber", "form", "primaryDocument", "acceptanceDateTime", "items")
    if any(not isinstance(recent.get(key), list) for key in required):
        raise SECError("Malformed SEC recent filing arrays")
    lengths = {len(recent[key]) for key in required}
    if len(lengths) != 1:
        raise SECError("SEC recent filing arrays have inconsistent lengths")
    return recent


def _qualifying(recent: dict, index: int) -> dict | None:
    """The filing at ``index`` when it is a supported non-earnings 8-K, else None."""
    form = recent["form"][index]
    item_value = recent["items"][index]
    if form not in FORMS or not isinstance(item_value, str):
        return None
    items = sorted(set(re.findall(r"\b\d{1,2}\.\d{2}\b", item_value)))
    if "2.02" in items or not {"7.01", "8.01"}.intersection(items):
        return None
    accession = recent["accessionNumber"][index]
    primary = recent["primaryDocument"][index]
    if not isinstance(accession, str) or not _ACCESSION.fullmatch(accession):
        return None
    if not isinstance(primary, str) or not _DOCUMENT.fullmatch(primary):
        return None
    acceptance = recent["acceptanceDateTime"][index]
    try:
        published = _published_at(acceptance)
    except SECError:
        return None
    return {
        "accession": accession,
        "form": form,
        "primary": primary,
        "items": items,
        "published_at": published,
        "acceptance": acceptance,
    }


def _supplied_first_seen(first_seen: str | Callable[[str], str], row: dict) -> str:
    value = first_seen(row["acceptance"]) if callable(first_seen) else first_seen
    if not isinstance(value, str) or ("T" not in value and " " not in value):
        raise SECError("first_seen must be a timestamp with a time and timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SECError("Invalid first_seen timestamp") from exc
    if parsed.tzinfo is None:
        raise SECError("first_seen needs an explicit timezone")
    if parsed < latest_acceptance(row["acceptance"]):
        raise SECError("first_seen cannot precede the latest reading of SEC acceptance")
    return _iso_utc(parsed)


def collect_filing(
    client: _SECClient,
    cik: str,
    accession: str,
    symbol: str,
    *,
    submissions: dict | None = None,
    mode: str = "forward",
    first_seen: str | Callable[[str], str] | None = None,
    verify_acceptance: bool = False,
) -> dict | None:
    """Collect one filing's selected document, or None when the filing does not qualify.

    ``submissions`` (the company's data.sec.gov JSON) saves one request when already
    fetched. Forward mode stamps actual receipt and refuses a supplied ``first_seen``.
    Historical mode requires one: a timestamp, or a callable given SEC's raw
    acceptanceDateTime; it may not precede the latest reading of that acceptance.
    Raises FilingNotFound when the accession is absent from the recent submissions.
    ``verify_acceptance`` also reads the filing's -index.htm: its 'Accepted' instant then
    sets published_at, accepted_at and after_hours (acceptance_basis "edgar_index_accepted").
    Otherwise published_at reads the JSON "Z" as UTC and after_hours uses the latest reading
    ("submissions_json_unverified"). A supplied first_seen is always checked against the
    latest reading, verified or not. At most 1 + 3 requests, or 1 + 4 when verifying, all
    through ``client``'s allowlist, budget and limiter.
    """
    cik = _check_cik(cik)
    symbol = _check_symbol(symbol)
    if not isinstance(accession, str) or not _ACCESSION.fullmatch(accession):
        raise SECError("Accession must look like 0000000000-00-000000")
    if mode not in MODES:
        raise SECError("mode must be forward or historical")
    if mode == "forward" and first_seen is not None:
        raise SECError("Forward collection records actual receipt; first_seen is not accepted")
    if mode == "historical" and first_seen is None:
        raise SECError("Historical collection requires an explicit first_seen assumption")
    normalized_cik = cik.zfill(10)
    data = (
        submissions
        if submissions is not None
        else client.get_json(f"https://data.sec.gov/submissions/CIK{normalized_cik}.json")
    )
    if not isinstance(data, dict):
        raise SECError("SEC submissions must be an object")
    reported = data.get("cik")
    if reported is not None and (
        not re.fullmatch(r"\d{1,10}", str(reported), re.A) or int(str(reported)) != int(cik)
    ):
        raise SECError("SEC submissions belong to a different CIK")
    recent = _recent_filings(data)
    try:
        index = recent["accessionNumber"].index(accession)
    except ValueError:
        raise FilingNotFound(f"Accession {accession} is not in recent SEC submissions") from None
    row = _qualifying(recent, index)
    if row is None:
        return None
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
    document, text, role, method = _filing_document(client, base, row["primary"])
    observed = _iso_utc(client.now())
    if not text.strip():
        # Empty source content is an error rather than a reason to exceed
        # the filing/request budget while looking for another candidate.
        raise SECError("Selected SEC document contains no readable text")
    seen = observed if first_seen is None else _supplied_first_seen(first_seen, row)
    if verify_acceptance:
        accepted = verified_acceptance(client.get(index_url(cik, accession)), row["acceptance"])
        published, basis = _iso_utc(accepted), ACCEPTANCE_BASES[0]
    else:
        accepted, basis = latest_acceptance(row["acceptance"]), ACCEPTANCE_BASES[1]
        published = row["published_at"]
    return {
        "id": f"sec:{accession}:{document}",
        "symbol": symbol.upper(),
        "published_at": published,
        "accepted_at": published,
        "after_hours": accepted.astimezone(EASTERN).time() >= AFTER_HOURS,
        "acceptance_basis": basis,
        "first_seen_at": seen,
        "source_url": base + document,
        "text": text[:MAX_TEXT_CHARS],
        "source_type": "sec",
        "cik": normalized_cik,
        "accession": accession,
        "form": row["form"],
        "items": row["items"],
        "document": document,
        "document_role": role,
        "selection_method": method,
        "text_truncated": len(text) > MAX_TEXT_CHARS,
        "timestamp_basis": "sec_acceptance_not_public_availability",
        "sec_acceptance_raw": row["acceptance"],
        "mode": mode,
    }


def collect_disclosures(
    cik: str,
    symbol: str,
    *,
    user_agent: str,
    limit: int = 5,
    timeout: float = 20,
) -> list[dict]:
    """Collect up to ``limit`` recent non-earnings operating disclosures.

    Explicit contact-bearing User-Agent is required. Each call is bounded to
    1 + 3*limit requests and at most five requests/second. Only SEC-hosted HTML or
    text is fetched; PDF-only exhibits and unknown item metadata are skipped or
    marked as cover-page fallbacks. Errors propagate so callers can fail closed.
    """
    _check_cik(cik)
    _check_symbol(symbol)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise SECError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    client = _SECClient(user_agent, timeout, 1 + 3 * limit)
    data = client.get_json(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json")
    recent = _recent_filings(data)
    records = []
    for index in range(min(len(recent["form"]), MAX_RECENT_FILINGS)):
        row = _qualifying(recent, index)
        if row is None:
            continue
        record = collect_filing(client, cik, row["accession"], symbol, submissions=data)
        if record is not None:
            records.append(record)
        if len(records) >= limit:
            break
    return records


# Private aliases kept until every caller patches the public seams (P0-26, #30).
# Patching an alias does not change what the module calls.
_transport = urlopen
_validate_url = validate_url
_utc_now = utc_now
