"""Pre-market brief and filing cards built only from records visible at ``now``.

Nothing first seen or decided after ``now`` appears. Every number is computed by code.
Filing text is untrusted: it appears only as short quotes that are verbatim substrings of
the stored text (at most CARD_QUOTE_CHARS per card), never a sentence shaped like a command
to the reader or to an AI agent, and every rendered value is escaped.
"""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime
from urllib.parse import quote, urlsplit

from . import evidence, paths
from .common import EASTERN, instant, sec_symbol, timestamp
from .market import _compatible, bars_for
from .providers import _NEGATIVE, _POSITIVE
from .security.quarantine import DIRECTIVE as _DIRECTIVE
from .security.sanitize import skeleton

MAX_FILINGS = 50
MAX_QUOTE_CHARS = 200
MIN_QUOTE_CHARS = 24
CARD_QUOTE_CHARS = 300
NOTIFY_TITLE_CHARS = 80
NOTIFY_BODY_CHARS = 240
MAX_ERROR_CHARS = 200
PHRASES = _POSITIVE + _NEGATIVE
OK_STATUSES = frozenset({"ok", "success", "skipped"})
GAP_ATTENTION_HOURS = 24  # an older coverage gap stays listed but no longer needs attention
# The service records a run at least every 15 minutes (idle or backed-off polling), so
# nothing for twice that long means it is not running, whatever the last run said.
STALE_MINUTES = 30
SERVICE_ATTENTION = "service"
NOTICE = "Research tool, not investment advice."
QUOTE_HEADING = "Quoted from the filing (company text):"
_BASIS_ORIGINS = {
    "builtin": "from the model registry",
    "declared": "declared in your config",
    "unregistered": "unregistered model",
}
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[“‘]?[A-Z0-9])")
_BOILERPLATE = re.compile(
    r"pursuant to the requirements|forward-looking statement|safe harbor|"
    r"incorporated (?:herein )?by reference|shall not be deemed|securities exchange act|"
    r"\bsignatures?\b",
    re.I,
)
_ITEM = re.compile(r"\d{1,2}\.\d{2}")
_FORM = re.compile(r"[0-9A-Z][0-9A-Z/-]{0,9}")
_WORDS = re.compile(r"[A-Za-z0-9 _-]{1,40}")
_UNSAFE = frozenset({"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"})


def _safe(text: str) -> bool:
    # Control, bidi-override and separator characters could restyle a notification or page.
    return all(unicodedata.category(ch) not in _UNSAFE for ch in text)


def _sentences(text: str) -> list[str]:
    result = []
    for line in re.finditer(r"[^\r\n]+", text):
        paragraph, start = line.group(), 0
        for gap in _BOUNDARY.finditer(paragraph):
            result.append(paragraph[start : gap.start()])
            start = gap.end()
        result.append(paragraph[start:])
    return [part.strip() for part in result if part.strip()]


def _clip(sentence: str, cap: int, anchor: int = 0, span: int = 0) -> str:
    """A verbatim window of at most ``cap`` characters that never ends mid-word."""
    if len(sentence) <= cap:
        return sentence
    start = max(0, min(anchor - (cap - span) // 2, len(sentence) - cap))
    piece = sentence[start : start + cap]
    if start > 0 and not sentence[start - 1].isspace():
        piece = piece.split(None, 1)[1] if len(piece.split(None, 1)) == 2 else ""
    if start + cap < len(sentence) and not sentence[start + cap].isspace():
        piece = piece.rsplit(None, 1)[0] if len(piece.rsplit(None, 1)) == 2 else ""
    return piece.strip()


def _bounded(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def verified_quotes(
    text: str,
    *,
    phrases=(),
    limit: int = 2,
    max_chars: int = MAX_QUOTE_CHARS,
    max_total: int | None = None,
) -> list[str]:
    """Short sentences copied verbatim from ``text``, preferring ones with a lexicon phrase.

    Sentences that address the reader or an AI agent, give orders or name tools are skipped.
    """
    if not isinstance(text, str):
        raise ValueError("Quote source text must be a string")
    _bounded(limit, "limit", 0, 10)
    _bounded(max_chars, "max_chars", MIN_QUOTE_CHARS, 1_000)
    if max_total is not None:
        _bounded(max_total, "max_total", 0, 10_000)
    if isinstance(phrases, str) or any(not isinstance(p, str) for p in phrases):
        raise ValueError("phrases must be a sequence of strings")
    wanted = [p.lower() for p in phrases if p.strip()]
    preferred, fallback = [], []
    for sentence in _sentences(text):
        if len(sentence) < MIN_QUOTE_CHARS or not _safe(sentence):
            continue
        # Match on the skeleton so homoglyph, fullwidth and zero-width spellings are caught (#12).
        if _DIRECTIVE.search(skeleton(sentence)):
            continue
        if not any(ch.isalpha() for ch in sentence):
            continue
        lowered = sentence.lower()
        hit = next((p for p in wanted if p in lowered), None)
        if hit is not None:
            preferred.append(_clip(sentence, max_chars, lowered.find(hit), len(hit)))
        elif not _BOILERPLATE.search(sentence):
            fallback.append(_clip(sentence, max_chars))
    quotes: list[str] = []
    used = 0
    for candidate in preferred + fallback:
        if len(quotes) >= limit:
            break
        if max_total is not None and used + len(candidate) > max_total:
            candidate = _clip(candidate, max_total - used) if max_total - used > 0 else ""
        # The final check is the guarantee: anything not found verbatim is dropped.
        if len(candidate) < MIN_QUOTE_CHARS or candidate in quotes or candidate not in text:
            continue
        quotes.append(candidate)
        used += len(candidate)
    return quotes


def _visible_forecasts(ledger, boundary: datetime) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for forecast in ledger.all("forecasts"):
        if instant(forecast["decision_at"]) <= boundary:
            result.setdefault(forecast["event_id"], []).append(forecast)
    return result


def _latest(forecasts: list[dict]) -> dict | None:
    if not forecasts:
        return None
    return max(
        forecasts, key=lambda f: (instant(f["decision_at"]), f.get("recorded_at", ""), f["id"])
    )


def _https(url: object) -> str | None:
    if (
        not isinstance(url, str)
        or len(url) > 500
        or not _safe(url)
        or any(c.isspace() for c in url)
    ):
        return None
    parts = urlsplit(url)
    return url if parts.scheme == "https" and parts.hostname and not parts.username else None


def _items(event: dict) -> list[str]:
    values = event.get("items")
    if not isinstance(values, list):
        return []
    return [v for v in values if isinstance(v, str) and _ITEM.fullmatch(v)][:20]


def _word(value: object, pattern: re.Pattern = _WORDS) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _action(forecast: dict | None) -> str | None:
    action = forecast.get("action") if forecast else None
    return action if action in evidence.ACTIONS else None


def _reasons(forecast: dict | None) -> list[str]:
    reasons = forecast.get("reasons") if forecast else None
    if not isinstance(reasons, list):
        return []
    return [r[:MAX_ERROR_CHARS] for r in reasons if isinstance(r, str)][:10]


def _expected(forecast: dict | None) -> float | None:
    value = forecast.get("expected_return") if forecast else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _unscored(ledger, event: dict, forecast: dict | None, boundary: datetime) -> str | None:
    """Why a filing has no decision, when the ledger can tell: its symbol has no usable bars."""
    if forecast is not None:
        return None
    usable = any(
        instant(bar["available_at"]) <= boundary and _compatible(bar, event["mode"])
        for bar in bars_for(ledger, event["symbol"])
    )
    return None if usable else f"not scored: no market data for {event['symbol']}"


def _filing(
    ledger, event: dict, forecast: dict | None, marked: set[str], boundary: datetime
) -> dict:
    return {
        "event_id": event["id"],
        "symbol": event["symbol"],
        "watchlist": sec_symbol(event["symbol"]) in marked,
        "form": _word(event.get("form"), _FORM),
        "items": _items(event),
        "mode": event["mode"],
        "published_at": event["published_at"],
        "first_seen_at": event["first_seen_at"],
        "source_url": _https(event.get("source_url")),
        "forecast_id": forecast["id"] if forecast else None,
        "decision_at": forecast["decision_at"] if forecast else None,
        "action": _action(forecast),
        "reasons": _reasons(forecast),
        "expected_return": _expected(forecast),
        "evidence": evidence.evidence_label(forecast) if forecast else None,
        "counts_as_evidence": evidence.is_evidence(forecast) if forecast else False,
        "unscored_reason": _unscored(ledger, event, forecast, boundary),
        "quotes": verified_quotes(event["text"], phrases=PHRASES, max_total=CARD_QUOTE_CHARS),
    }


def _short(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch if _safe(ch) else " " for ch in value).strip()
    return cleaned[:MAX_ERROR_CHARS] or None


def health(ledger, *, now: str) -> dict:
    """Latest run per job started by ``now``; unknown statuses need attention (fail closed).

    Runs that stopped arriving are stale: the last ones may all be ok while nothing runs.
    """
    boundary = instant(now)
    latest: dict[str, tuple[datetime, dict]] = {}
    last_activity: datetime | None = None
    for run in ledger.all("runs"):
        job, started = run.get("job"), run.get("started_at")
        if not isinstance(job, str) or not job or not isinstance(started, str):
            continue
        try:
            moment = instant(started)
        except ValueError:
            continue
        if moment > boundary:
            continue
        if job not in latest or moment > latest[job][0]:
            latest[job] = (moment, run)
        ended = _ended(run, moment, boundary)
        last_activity = ended if last_activity is None else max(last_activity, ended)
    jobs = {}
    for job, (moment, run) in sorted(latest.items()):
        raw = run.get("counts")
        counts = raw if isinstance(raw, dict) else {}
        jobs[_short(job) or "job"] = {
            "status": _short(run.get("status")) or "unknown",
            "started_at": timestamp(run["started_at"]),
            "finished_at": _short(run.get("finished_at")),
            "age_minutes": int((boundary - moment).total_seconds() // 60),
            "error": _short(run.get("error")),
            "counts": {
                str(key)[:40]: value
                for key, value in counts.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            },
        }
    attention = sorted(
        job
        for job, run in jobs.items()
        if run["status"] not in OK_STATUSES and not _settled_gap(run, boundary)
    )
    idle = (
        int((boundary - last_activity).total_seconds() // 60) if last_activity is not None else None
    )
    stale = idle is not None and idle >= STALE_MINUTES
    if stale:
        attention.append(SERVICE_ATTENTION)
    state = "idle" if not jobs else "attention" if attention else "ok"
    return {
        "as_of": timestamp(now),
        "state": state,
        "ok": state == "ok",
        "attention": attention,
        "jobs": jobs,
        "last_run_at": timestamp(last_activity.isoformat()) if last_activity else None,
        "stale": stale,
        "stale_note": (
            f"No background runs for {duration(idle)}; is the service running?"
            if idle is not None and stale
            else None
        ),
    }


def _ended(run: dict, started: datetime, boundary: datetime) -> datetime:
    # A run is recorded when it ends: a readable end no later than now is the latest sign of life.
    finished = run.get("finished_at")
    try:
        ended = instant(finished) if isinstance(finished, str) else started
    except ValueError:
        return started
    return ended if started <= ended <= boundary else started


def duration(minutes: int) -> str:
    if minutes < 90:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} days"


def _settled_gap(run: dict, boundary: datetime) -> bool:
    try:
        ended = instant(run["finished_at"] or "")
    except ValueError:
        return False
    return (
        run["status"] == "gap" and (boundary - ended).total_seconds() > GAP_ATTENTION_HOURS * 3600
    )


def search(
    ledger,
    *,
    now: str,
    ticker: str | None = None,
    since: str | None = None,
    limit: int = 10,
    watchlist: list[str] | tuple[str, ...] = (),
) -> dict:
    """Filings first seen by ``now`` (and after ``since``), newest first; synthetic excluded.
    ``watchlist`` only marks filings, as in the brief."""
    boundary = instant(now)
    _bounded(limit, "limit", 1, MAX_FILINGS)
    marked = _marked(watchlist)
    # Compared in SEC's share-class form, so BRK.B finds the BRK-B filings SEC maps.
    wanted = sec_symbol(ticker) if ticker is not None else None
    start = instant(since) if since is not None else None
    events = [
        e
        for e in ledger.all("disclosures")
        if e["mode"] != "synthetic"
        and instant(e["first_seen_at"]) <= boundary
        and (start is None or instant(e["first_seen_at"]) > start)
        and (wanted is None or sec_symbol(e["symbol"]) == wanted)
    ]
    events.sort(key=lambda e: (instant(e["first_seen_at"]), e["id"]), reverse=True)
    forecasts = _visible_forecasts(ledger, boundary)
    return {
        "as_of": timestamp(now),
        "symbol": wanted,
        "since": timestamp(since) if since is not None else None,
        "total": len(events),
        "filings": [
            _filing(ledger, e, _latest(forecasts.get(e["id"], [])), marked, boundary)
            for e in events[:limit]
        ],
        "notice": NOTICE,
    }


def _marked(watchlist: list[str] | tuple[str, ...]) -> set[str]:
    if isinstance(watchlist, str):
        raise ValueError("watchlist must be a list of symbols")
    return {sec_symbol(name) for name in watchlist}


def compose(ledger, *, now: str, since: str | None, watchlist: list[str]) -> dict:
    """Filings first seen in (since, now], watchlist first, newest first; synthetic excluded."""
    boundary = instant(now)
    start = instant(since) if since is not None else None
    if start is not None and start >= boundary:
        raise ValueError("since must be earlier than now")
    marked = _marked(watchlist)
    events = [
        e
        for e in ledger.all("disclosures")
        if e["mode"] != "synthetic"
        and instant(e["first_seen_at"]) <= boundary
        and (start is None or instant(e["first_seen_at"]) > start)
    ]
    events.sort(key=lambda e: (instant(e["first_seen_at"]), e["id"]), reverse=True)
    events.sort(key=lambda e: sec_symbol(e["symbol"]) not in marked)
    forecasts = _visible_forecasts(ledger, boundary)
    board = evidence.scoreboard(ledger, as_of=now)
    return {
        "generated_at": timestamp(now),
        "since": timestamp(since) if since is not None else None,
        "watchlist": sorted(marked),
        "total": len(events),
        "truncated": len(events) > MAX_FILINGS,
        "filings": [
            _filing(ledger, e, _latest(forecasts.get(e["id"], [])), marked, boundary)
            for e in events[:MAX_FILINGS]
        ],
        "scoreboard": {
            key: board[key]
            for key in ("status", "label", "calls", "pending_calls", "interval", "gate_sha256")
        }
        | {"min_matured_calls": board["gate"]["min_matured_calls"]},
        "health": health(ledger, now=now),
        "notice": NOTICE,
    }


def _outcome(forecast: dict, outcome: dict | None, boundary: datetime) -> dict | None:
    if outcome is None or evidence.label_available_at(outcome) > boundary:
        return None
    result = {
        key: outcome[key]
        for key in (
            "entry_at",
            "outcome_at",
            "label_available_at",
            "gross_return",
            "benchmark_return",
            "target",
        )
        if key in outcome
    }
    result["net_return"] = evidence.net_return(forecast, outcome)
    return result


def _decision(ledger, forecast: dict, boundary: datetime) -> dict:
    names, values = forecast.get("feature_names") or [], forecast.get("features") or []
    raw = forecast.get("market")
    market = raw if isinstance(raw, dict) else {}
    frozen = forecast.get("strategy")
    return {
        "id": forecast["id"],
        "decision_at": forecast["decision_at"],
        "mode": forecast.get("mode"),
        "evidence": evidence.evidence_label(forecast),
        "counts_as_evidence": evidence.is_evidence(forecast),
        "evidence_basis": _basis(forecast.get("eligibility_basis")),
        "provider": _word(forecast.get("provider")),
        "resolved_model": _short(forecast.get("resolved_model")),
        "calibrator_id": _short(forecast.get("calibrator_id")),
        "benchmark": _word(frozen.get("benchmark")) if isinstance(frozen, dict) else None,
        "action": _action(forecast),
        "reasons": _reasons(forecast),
        "expected_return": _expected(forecast),
        "features": {
            str(name): float(value)
            for name, value in zip(names, values)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
        "market": {
            key: market[key]
            for key in ("session", "price", "dollar_volume", "reaction", "momentum", "volatility")
            if key in market
        },
        "outcome": _outcome(forecast, ledger.get("outcomes", forecast["id"]), boundary),
    }


def _basis(value: object) -> dict | None:
    """The registry facts frozen with a forecast's label; ``declared`` came from config."""
    if not isinstance(value, dict):
        return None
    return {
        "origin": _word(value.get("origin")),
        "training_cutoff": _short(value.get("training_cutoff")),
        "source": _short(value.get("source")),
    }


def filing_card(ledger, event_id: str, *, now: str) -> dict:
    """One filing and every decision on it visible at ``now``; no raw filing text."""
    boundary = instant(now)
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a nonempty string")
    event = ledger.get("disclosures", event_id)
    # The same message for unknown and not-yet-seen filings: no oracle for the future.
    if event is None or instant(event["first_seen_at"]) > boundary:
        raise ValueError(f"No filing {event_id!r} is visible at {timestamp(now)}")
    forecasts = sorted(
        _visible_forecasts(ledger, boundary).get(event_id, []),
        key=lambda f: (instant(f["decision_at"]), f["id"]),
        reverse=True,
    )
    return {
        "as_of": timestamp(now),
        "event_id": event["id"],
        "symbol": event["symbol"],
        "form": _word(event.get("form"), _FORM),
        "items": _items(event),
        "mode": event["mode"],
        "published_at": event["published_at"],
        "first_seen_at": event["first_seen_at"],
        "source_url": _https(event.get("source_url")),
        "document_role": _word(event.get("document_role")),
        "selection_method": _word(event.get("selection_method")),
        "quotes": verified_quotes(event["text"], phrases=PHRASES, max_total=CARD_QUOTE_CHARS),
        "decisions": [_decision(ledger, f, boundary) for f in forecasts],
        "notice": NOTICE,
    }


def et_time(value: str | None) -> str:
    if not value:
        return "—"
    return instant(value).astimezone(EASTERN).strftime("%a %d %b %Y, %H:%M ET")


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2%}"


def _notification_names(filings: list[dict]) -> list[str]:
    return [f"{f['symbol']} {f['action'] or 'unscored'}" for f in filings]


def render_text(brief: dict) -> tuple[str, str]:
    """(title, body) for a notification; code-built values only, never filing text."""
    filings, total = brief["filings"], brief["total"]
    title = f"{paths.APP_NAME} brief: {total} new filing{'' if total == 1 else 's'}"
    marked = [f for f in filings if f["watchlist"]]
    shown = marked or filings
    board, jobs = brief["scoreboard"], brief["health"]
    tail = [f"Evidence: {board['status']} ({board['calls']}/{board['min_matured_calls']} calls)."]
    if jobs["state"] == "attention":
        tail.append(f"Check jobs: {', '.join(jobs['attention'])}."[:80])
    tail.append("Research only, not advice.")
    for keep in range(len(shown), -1, -1):
        names = _notification_names(shown[:keep])
        rest = total - keep
        if not total:
            lead = "No new filings."
        elif keep == 0:
            lead = f"{total} new filing{'' if total == 1 else 's'}."
        else:
            prefix = "Watchlist: " if marked else ""
            lead = prefix + ", ".join(names) + (f", +{rest} more." if rest else ".")
        body = " ".join([lead, *tail])
        if len(body) <= NOTIFY_BODY_CHARS:
            return title[:NOTIFY_TITLE_CHARS], body
    return title[:NOTIFY_TITLE_CHARS], body[: NOTIFY_BODY_CHARS - 1] + "…"


def escape(value: object) -> str:
    """Any value as HTML text, safe in element content and quoted attributes."""
    return html.escape(str(value), quote=True)


def _pill(action: str | None) -> str:
    name = action or "unscored"
    return f'<span class="pill a-{escape(name.lower())}">{escape(name)}</span>'


def _source(url: str | None) -> str:
    if url is None:
        return '<span class="muted">source link unavailable</span>'
    host = urlsplit(url).hostname or "source"
    return f'<a href="{escape(url)}" rel="noopener noreferrer">Source ({escape(host)})</a>'


def _quotes(quotes: list[str], heading: str = "") -> str:
    if not quotes:
        return '<p class="muted">No short verified quote available.</p>'
    # Beside an action pill, an unattributed sentence could read as this tool's advice.
    lead = f'<p class="meta">{escape(heading)}</p>' if heading else ""
    return lead + "".join(f'<blockquote class="quote">{escape(q)}</blockquote>' for q in quotes)


def _meta(item: dict) -> str:
    parts = [escape(item["form"] or "Filing")]
    if item["items"]:
        parts.append("Items " + escape(", ".join(item["items"])))
    parts.append("first seen " + escape(et_time(item["first_seen_at"])))
    if item["mode"] != "forward":
        parts.append(escape(f"{item['mode']} record"))
    return " · ".join(parts)


def _card(item: dict) -> str:
    link = f"/filing/{quote(item['event_id'], safe='')}"
    reasons = "".join(f"<li>{escape(r)}</li>" for r in item["reasons"])
    estimate = (
        f"Expected excess return {escape(percent(item['expected_return']))}"
        if item["expected_return"] is not None
        else "No calibrated estimate"
    )
    label = item["evidence"] or item["unscored_reason"] or "not scored yet"
    return (
        f'<article class="card{" watch" if item["watchlist"] else ""}">'
        f'<div class="card-top"><span class="sym">{escape(item["symbol"])}</span>'
        f'{_pill(item["action"])}<span class="tag">{escape(label)}</span></div>'
        f'<p class="meta">{_meta(item)}</p>'
        f'<p class="num">{estimate}</p>'
        + (f'<ul class="reasons">{reasons}</ul>' if reasons else "")
        + _quotes(item["quotes"], QUOTE_HEADING)
        + f'<p class="links"><a href="{escape(link)}">Details</a> · {_source(item["source_url"])}</p>'
        "</article>"
    )


def _cards(items: list[dict], empty: str) -> str:
    return "".join(_card(i) for i in items) if items else f'<p class="muted">{escape(empty)}</p>'


def _health_list(jobs: dict) -> str:
    if not jobs["jobs"]:
        return '<p class="muted">No background runs recorded yet.</p>'
    rows = "".join(
        f'<li><span class="job">{escape(job)}</span> <span class="st st-{"ok" if run["status"] in OK_STATUSES else "bad"}">'
        f'{escape(run["status"])}</span> <span class="muted">{escape(et_time(run["started_at"]))}</span></li>'
        for job, run in jobs["jobs"].items()
    )
    stale = f'<p class="st-bad">{escape(jobs["stale_note"])}</p>' if jobs["stale_note"] else ""
    return f'{stale}<ul class="jobs">{rows}</ul>'


def render_html(brief: dict) -> str:
    """The brief as an HTML fragment; every value is escaped."""
    window = (
        f"after {escape(et_time(brief['since']))}" if brief["since"] else "so far"
    ) + f" through {escape(et_time(brief['generated_at']))}"
    marked = [f for f in brief["filings"] if f["watchlist"]]
    others = [f for f in brief["filings"] if not f["watchlist"]]
    if brief["watchlist"]:
        lists = (
            "<h2>Watchlist</h2>"
            + _cards(marked, "No new watchlist filings.")
            + "<h2>Other filings</h2>"
            + _cards(others, "No other new filings.")
        )
    else:
        lists = "<h2>Filings</h2>" + _cards(others, "No new filings in this window.")
    more = (
        f'<p class="muted">Showing {len(brief["filings"])} of {brief["total"]} filings.</p>'
        if brief["truncated"]
        else ""
    )
    return (
        '<section class="brief">'
        "<h1>Pre-market brief</h1>"
        f'<p class="meta">Filings first seen {window}.</p>'
        f'<section class="panel"><h2>Evidence so far</h2><p>{escape(brief["scoreboard"]["label"])}</p>'
        '<p class="links"><a href="/scoreboard">Scoreboard</a></p></section>'
        f"{lists}{more}"
        f'<section class="panel"><h2>Background jobs</h2>{_health_list(brief["health"])}'
        '<p class="links"><a href="/health">Health</a></p></section>'
        "</section>"
    )


def _number(value: object, digits: int = 3) -> str:
    return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "—"


def _table(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<tr><th>{escape(k)}</th><td class="num">{escape(v)}</td></tr>' for k, v in rows
    )
    return f'<table class="kv">{body}</table>'


def _decision_html(decision: dict) -> str:
    rows = [(name, _number(value)) for name, value in decision["features"].items()]
    market = decision["market"]
    if "price" in market:
        rows.append(("last close", f"${_number(market['price'], 2)}"))
    if "dollar_volume" in market:
        rows.append(("20-day dollar volume", f"${market['dollar_volume'] / 1e6:,.1f}M"))
    outcome = decision["outcome"]
    if outcome is None:
        result = '<p class="muted">Outcome not matured yet.</p>'
    else:
        # A move, not a result: no position is ever taken, and WATCH or PASS makes no call.
        benchmark = decision["benchmark"] or "benchmark"
        outcome_rows = [
            (
                f"stock minus {benchmark} over the label window (no position taken)",
                percent(outcome.get("target")),
            ),
            ("entry", et_time(outcome.get("entry_at"))),
            ("exit", et_time(outcome.get("outcome_at"))),
        ]
        if outcome["net_return"] is not None:
            outcome_rows.insert(1, ("net of costs", percent(outcome["net_return"])))
        result = "<h3>Outcome</h3>" + _table(outcome_rows)
    reasons = "".join(f"<li>{escape(r)}</li>" for r in decision["reasons"])
    estimate = (
        percent(decision["expected_return"])
        if decision["expected_return"] is not None
        else "no calibrated estimate"
    )
    source = " · ".join(escape(v) for v in (decision["provider"], decision["resolved_model"]) if v)
    return (
        '<section class="card">'
        f'<div class="card-top">{_pill(decision["action"])}<span class="tag">{escape(decision["evidence"])}</span></div>'
        f'<p class="meta">Decided {escape(et_time(decision["decision_at"]))} · {source}</p>'
        + _basis_html(decision["evidence_basis"])
        + f'<p class="num">Expected excess return: {escape(estimate)}</p>'
        + (f'<ul class="reasons">{reasons}</ul>' if reasons else "")
        + (_table(rows) if rows else "")
        + result
        + "</section>"
    )


def _basis_html(basis: dict | None) -> str:
    if basis is None:
        return ""
    cutoff = basis["training_cutoff"]
    known = f"training cutoff {cutoff}" if cutoff else "no training cutoff on record"
    origin = _BASIS_ORIGINS.get(basis["origin"] or "", "origin unknown")
    return f'<p class="meta">Evidence basis: {escape(known)} · {escape(origin)}</p>'


def render_filing_html(card: dict) -> str:
    """One filing card as an HTML fragment; every value is escaped."""
    item = {**card, "watchlist": False}
    decisions = (
        "".join(_decision_html(d) for d in card["decisions"])
        if card["decisions"]
        else '<p class="muted">No decision recorded yet.</p>'
    )
    return (
        '<article class="filing">'
        f'<h1><span class="sym">{escape(card["symbol"])}</span></h1>'
        f'<p class="meta">{_meta(item)} · accepted {escape(et_time(card["published_at"]))}</p>'
        f'<p class="links">{_source(card["source_url"])}</p>'
        f"<h2>From the filing</h2>{_quotes(card['quotes'])}"
        f"<h2>Decisions</h2>{decisions}"
        "</article>"
    )
