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
from zoneinfo import ZoneInfo

from . import evidence, paths
from .common import instant, sec_symbol, timestamp
from .providers import _NEGATIVE, _POSITIVE

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
NOTICE = "Research tool, not investment advice."
MARKET_ZONE = ZoneInfo("America/New_York")
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[“‘]?[A-Z0-9])")
_BOILERPLATE = re.compile(
    r"pursuant to the requirements|forward-looking statement|safe harbor|"
    r"incorporated (?:herein )?by reference|shall not be deemed|securities exchange act|"
    r"\bsignatures?\b",
    re.I,
)
# The filer writes the text and the lexicon picks which sentences get quoted, so a filer could
# get an instruction quoted to an assistant that reads the card (and may hold trading tools).
# Best effort, erring toward dropping: a dropped sentence only means another one is quoted.
_DIRECTIVE = re.compile(
    r"\b(?:you|your|yours|yourself|assistants?|chatbots?|llms?|language models?|"
    r"ai (?:agents?|assistants?|models?)|prompts?|instructions?|tool[ _-]?(?:calls?|use)|"
    r"ignore|disregard|forget|override)\b"
    r"|\b(?:system|user|human|assistant|ai|agent|model|developer)\s*:"
    r"|^\W*(?:please\s+)?(?:buy|sell|short|place|submit|execute|cancel|use|call|invoke|tell|"
    r"send|do not|don't|never|always)(?![\w-])"
    r"|\b(?:buy|sell|limit|market|stop)\s+orders?\b"
    r"|\bplac(?:e|ing)\s+(?:an?\s+|the\s+)?(?:\w+\s+)?(?:orders?|trades?)\b"
    r"|\b(?:use|call|invoke)\b[^.!?]{0,40}\btools?\b"
    r"|\b[a-z0-9]+_[a-z0-9_]+\b"  # snake_case reads as a tool or function name
    r"|<\||\|>",
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
        if len(sentence) < MIN_QUOTE_CHARS or not _safe(sentence) or _DIRECTIVE.search(sentence):
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


def _filing(event: dict, forecast: dict | None, marked: set[str]) -> dict:
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
        "quotes": verified_quotes(event["text"], phrases=PHRASES, max_total=CARD_QUOTE_CHARS),
    }


def _short(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch if _safe(ch) else " " for ch in value).strip()
    return cleaned[:MAX_ERROR_CHARS] or None


def health(ledger, *, now: str) -> dict:
    """Latest run per job started by ``now``; unknown statuses need attention (fail closed)."""
    boundary = instant(now)
    latest: dict[str, tuple[datetime, dict]] = {}
    for run in ledger.all("runs"):
        job, started = run.get("job"), run.get("started_at")
        if not isinstance(job, str) or not job or not isinstance(started, str):
            continue
        try:
            moment = instant(started)
        except ValueError:
            continue
        if moment <= boundary and (job not in latest or moment > latest[job][0]):
            latest[job] = (moment, run)
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
    state = "idle" if not jobs else "attention" if attention else "ok"
    return {
        "as_of": timestamp(now),
        "state": state,
        "ok": state == "ok",
        "attention": attention,
        "jobs": jobs,
    }


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
) -> dict:
    """Filings first seen by ``now`` (and after ``since``), newest first; synthetic excluded."""
    boundary = instant(now)
    _bounded(limit, "limit", 1, MAX_FILINGS)
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
        "filings": [_filing(e, _latest(forecasts.get(e["id"], [])), set()) for e in events[:limit]],
        "notice": NOTICE,
    }


def compose(ledger, *, now: str, since: str | None, watchlist: list[str]) -> dict:
    """Filings first seen in (since, now], watchlist first, newest first; synthetic excluded."""
    boundary = instant(now)
    start = instant(since) if since is not None else None
    if start is not None and start >= boundary:
        raise ValueError("since must be earlier than now")
    if isinstance(watchlist, str):
        raise ValueError("watchlist must be a list of symbols")
    marked = {sec_symbol(name) for name in watchlist}
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
            _filing(e, _latest(forecasts.get(e["id"], [])), marked) for e in events[:MAX_FILINGS]
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
    return {
        "id": forecast["id"],
        "decision_at": forecast["decision_at"],
        "mode": forecast.get("mode"),
        "evidence": evidence.evidence_label(forecast),
        "counts_as_evidence": evidence.is_evidence(forecast),
        "provider": _word(forecast.get("provider")),
        "resolved_model": _short(forecast.get("resolved_model")),
        "calibrator_id": _short(forecast.get("calibrator_id")),
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
    return instant(value).astimezone(MARKET_ZONE).strftime("%a %d %b %Y, %H:%M ET")


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


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _pill(action: str | None) -> str:
    name = action or "unscored"
    return f'<span class="pill a-{_e(name.lower())}">{_e(name)}</span>'


def _source(url: str | None) -> str:
    if url is None:
        return '<span class="muted">source link unavailable</span>'
    host = urlsplit(url).hostname or "source"
    return f'<a href="{_e(url)}" rel="noopener noreferrer">Source ({_e(host)})</a>'


def _quotes(quotes: list[str]) -> str:
    if not quotes:
        return '<p class="muted">No short verified quote available.</p>'
    return "".join(f'<blockquote class="quote">{_e(q)}</blockquote>' for q in quotes)


def _meta(item: dict) -> str:
    parts = [_e(item["form"] or "Filing")]
    if item["items"]:
        parts.append("Items " + _e(", ".join(item["items"])))
    parts.append("first seen " + _e(et_time(item["first_seen_at"])))
    if item["mode"] != "forward":
        parts.append(_e(f"{item['mode']} record"))
    return " · ".join(parts)


def _card(item: dict) -> str:
    link = f"/filing/{quote(item['event_id'], safe='')}"
    reasons = "".join(f"<li>{_e(r)}</li>" for r in item["reasons"])
    estimate = (
        f"Expected excess return {_e(percent(item['expected_return']))}"
        if item["expected_return"] is not None
        else "No calibrated estimate"
    )
    label = item["evidence"] or "not scored yet"
    return (
        f'<article class="card{" watch" if item["watchlist"] else ""}">'
        f'<div class="card-top"><span class="sym">{_e(item["symbol"])}</span>'
        f'{_pill(item["action"])}<span class="tag">{_e(label)}</span></div>'
        f'<p class="meta">{_meta(item)}</p>'
        f'<p class="num">{estimate}</p>'
        + (f'<ul class="reasons">{reasons}</ul>' if reasons else "")
        + _quotes(item["quotes"])
        + f'<p class="links"><a href="{_e(link)}">Details</a> · {_source(item["source_url"])}</p>'
        "</article>"
    )


def _cards(items: list[dict], empty: str) -> str:
    return "".join(_card(i) for i in items) if items else f'<p class="muted">{_e(empty)}</p>'


def _health_list(jobs: dict) -> str:
    if not jobs["jobs"]:
        return '<p class="muted">No background runs recorded yet.</p>'
    rows = "".join(
        f'<li><span class="job">{_e(job)}</span> <span class="st st-{"ok" if run["status"] in OK_STATUSES else "bad"}">'
        f'{_e(run["status"])}</span> <span class="muted">{_e(et_time(run["started_at"]))}</span></li>'
        for job, run in jobs["jobs"].items()
    )
    return f'<ul class="jobs">{rows}</ul>'


def render_html(brief: dict) -> str:
    """The brief as an HTML fragment; every value is escaped."""
    window = (
        f"after {_e(et_time(brief['since']))}" if brief["since"] else "so far"
    ) + f" through {_e(et_time(brief['generated_at']))}"
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
        f'<section class="panel"><h2>Evidence so far</h2><p>{_e(brief["scoreboard"]["label"])}</p>'
        '<p class="links"><a href="/scoreboard">Scoreboard</a></p></section>'
        f"{lists}{more}"
        f'<section class="panel"><h2>Background jobs</h2>{_health_list(brief["health"])}'
        '<p class="links"><a href="/health">Health</a></p></section>'
        "</section>"
    )


def _number(value: object, digits: int = 3) -> str:
    return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "—"


def _table(rows: list[tuple[str, str]]) -> str:
    body = "".join(f'<tr><th>{_e(k)}</th><td class="num">{_e(v)}</td></tr>' for k, v in rows)
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
        outcome_rows = [
            ("benchmark-relative return", percent(outcome.get("target"))),
            ("entry", et_time(outcome.get("entry_at"))),
            ("exit", et_time(outcome.get("outcome_at"))),
        ]
        if outcome["net_return"] is not None:
            outcome_rows.insert(1, ("net of costs", percent(outcome["net_return"])))
        result = "<h3>Outcome</h3>" + _table(outcome_rows)
    reasons = "".join(f"<li>{_e(r)}</li>" for r in decision["reasons"])
    estimate = (
        percent(decision["expected_return"])
        if decision["expected_return"] is not None
        else "no calibrated estimate"
    )
    source = " · ".join(_e(v) for v in (decision["provider"], decision["resolved_model"]) if v)
    return (
        '<section class="card">'
        f'<div class="card-top">{_pill(decision["action"])}<span class="tag">{_e(decision["evidence"])}</span></div>'
        f'<p class="meta">Decided {_e(et_time(decision["decision_at"]))} · {source}</p>'
        f'<p class="num">Expected excess return: {_e(estimate)}</p>'
        + (f'<ul class="reasons">{reasons}</ul>' if reasons else "")
        + (_table(rows) if rows else "")
        + result
        + "</section>"
    )


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
        f'<h1><span class="sym">{_e(card["symbol"])}</span></h1>'
        f'<p class="meta">{_meta(item)} · accepted {_e(et_time(card["published_at"]))}</p>'
        f'<p class="links">{_source(card["source_url"])}</p>'
        f"<h2>From the filing</h2>{_quotes(card['quotes'])}"
        f"<h2>Decisions</h2>{decisions}"
        "</article>"
    )
