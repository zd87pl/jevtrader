"""OpenAI-compatible local engines (Ollama, LM Studio, mlx_lm), reached on loopback only.

Filing text goes only to 127.0.0.1, localhost or [::1], never through a proxy or a
redirect, so it cannot leave the machine. No key is sent and nothing is billed. Answers
are held to the paid providers' bounds and retried once when malformed; the values
describe source text and are never win probabilities.
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlsplit

from .providers import (
    _BLOCK_NOTE,
    _EVIDENCE_INSTRUCTIONS,
    _FEATURE_SCALE,
    _FEATURE_SCHEMA,
    _QUESTION_KEYS,
    MAX_RESPONSE_BYTES,
    MAX_TEXT_CHARS,
    ProviderError,
    ProviderInputError,
    ProviderValidationError,
    Transport,
    _data_message,
    _json_text,
    _NoRedirect,
    _object,
    _questions_text,
    _result,
    _string,
)


DEFAULT_MODEL = "gpt-oss:120b"
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"  # Ollama; LM Studio listens on :1234
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_TIMEOUT = 3600.0
MAX_OUTPUT_TOKENS = 4_096
_NETLOC = re.compile(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?")
_PATH = re.compile(r"(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*/?")
_INSTRUCTIONS = (
    f"{_EVIDENCE_INSTRUCTIONS}\n{_BLOCK_NOTE}\n{_FEATURE_SCALE} "
    "Reply with only a JSON object containing exactly these four numbers."
)
_TEMPERATURE = 0
_RETRY_NOTE = (
    "Your previous reply did not match the required JSON schema. Reply with only one JSON "
    "object with exactly direction, materiality, novelty and uncertainty as numbers within "
    "their stated ranges."
)
_PROBE_CURRENT = "The company raised guidance, citing strong demand and record revenue."
_PROBE_PREVIOUS = "The company reported quarterly results in line with its prior outlook."
_PROBE_QUESTIONS = {
    "direction": "Are the company's business prospects improving or deteriorating?",
    "materiality": "Is there a material change to the company's business prospects?",
    "novelty": "Does the current text add substantive information beyond the previous text?",
}


class EngineUnreachable(ProviderError):
    """No usable answer arrived from the loopback address (refused, reset or timed out)."""


def normalize_base_url(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    try:
        parts = urlsplit(text)
        valid = (
            bool(text)
            and not any(c.isspace() or not c.isprintable() or c in "?#@\\%" for c in text)
            and parts.scheme in ("http", "https")
            and parts.hostname in LOOPBACK_HOSTS
            and parts.port != 0
            and _NETLOC.fullmatch(parts.netloc.lower()) is not None
            and _PATH.fullmatch(parts.path) is not None
        )
    except ValueError:
        valid = False
    if not valid:
        # A local model must never become a path for filing text to leave the machine.
        raise ProviderInputError(
            "Local engine URL must be http(s) on 127.0.0.1, localhost or [::1] "
            "with a plain path and no credentials, query or fragment"
        )
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def _model(value: object) -> str:
    text = _string(value, "model", limit=200)
    if not text.isprintable() or text != text.strip():
        raise ProviderValidationError("model must be printable without surrounding spaces")
    return text


def _timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= MAX_TIMEOUT
    ):
        raise ProviderValidationError(
            f"timeout must be a number of seconds in (0, {MAX_TIMEOUT:g}]"
        )
    return float(value)


def _inputs(model: object, current: object, previous: object, questions: object) -> None:
    try:
        _model(model)
        if not isinstance(current, str) or not current.strip():
            raise ProviderValidationError("current_text must be a nonempty string")
        if not isinstance(previous, str):
            raise ProviderValidationError("previous_text must be a string")
        if len(current) + len(previous) > MAX_TEXT_CHARS:
            raise ProviderValidationError(
                f"Combined document text exceeds {MAX_TEXT_CHARS} characters; select relevant excerpts explicitly"
            )
        mapping = _object(questions, "questions")
        if set(mapping) != _QUESTION_KEYS:
            raise ProviderValidationError(
                "questions must contain exactly direction, materiality, novelty"
            )
        for name in sorted(_QUESTION_KEYS):
            _string(mapping[name], f"questions.{name}")
    except ProviderInputError:
        raise
    except ProviderValidationError as exc:
        raise ProviderInputError(str(exc)) from None


def http_json(url: str, payload: dict | None, _key: str, timeout: float) -> dict:
    """Default transport: GET when payload is None, else POST JSON. Never sends a key."""
    url = normalize_base_url(url)
    data = None if payload is None else _json_text(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="GET" if data is None else "POST"
    )
    # An empty ProxyHandler also ignores macOS system proxies; a proxy would carry text off-host.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise ProviderError(f"Local engine HTTP error {status}") from None
    except Exception as exc:
        if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
            raise EngineUnreachable(f"Local engine did not respond within {timeout:g} s") from None
        raise EngineUnreachable("Local engine connection failed; is it running?") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProviderValidationError("Local engine response exceeds the size limit")
    try:
        result = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise ProviderValidationError("Local engine returned invalid JSON") from None
    return _object(result, "response")


def _call(
    transport: Callable[..., dict] | None, url: str, payload: dict | None, timeout: float
) -> dict:
    try:
        raw = (transport or http_json)(url, payload, "", timeout)
    except ProviderError:
        raise
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise ProviderError(f"Local engine HTTP error {status}") from None
    except Exception:
        # Injected transport text is not ours to repeat.
        raise EngineUnreachable("Local engine transport failed") from None
    _object(raw, "response")
    _json_text(raw)
    return raw


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ProviderValidationError("Local model output repeats a key")
    return result


def _nonfinite(_: str) -> float:
    raise ProviderValidationError("Local model output must contain only finite numbers")


def _completion(raw: dict) -> tuple[dict, str, int]:
    if raw.get("error") is not None:
        raise ProviderError("Local engine reported an error")
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderValidationError("Local engine response must contain exactly one choice")
    choice = _object(choices[0], "choice")
    message = _object(choice.get("message"), "choice.message")
    if message.get("refusal") not in (None, ""):
        raise ProviderError("Local model refused this extraction")
    if choice.get("finish_reason") not in (None, "stop"):
        raise ProviderValidationError("Local model did not finish its answer")
    if message.get("role") != "assistant" or message.get("tool_calls"):
        raise ProviderValidationError("Unexpected local model message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderValidationError("Local model returned no answer text")
    try:
        features = json.loads(content, object_pairs_hook=_unique, parse_constant=_nonfinite)
    except (json.JSONDecodeError, RecursionError):
        raise ProviderValidationError("Local model output was not valid JSON") from None
    _object(features, "local model output")
    model = _string(raw.get("model"), "response.model", limit=200)
    tokens = _object(raw.get("usage"), "response.usage").get("prompt_tokens")
    if type(tokens) is not int or tokens < 0:
        raise ProviderValidationError("response.usage.prompt_tokens must be a nonnegative integer")
    return features, model, tokens


def request_body(model: str, current: str, previous: str, questions: dict) -> dict:
    """Questions in the system message; filing text only in tagged blocks in the user message."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": f"{_INSTRUCTIONS}\n{_questions_text(questions)}"},
            {"role": "user", "content": _data_message(current, previous)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "financial_text_features",
                "strict": True,
                "schema": copy.deepcopy(_FEATURE_SCHEMA),  # fresh per request
            },
        },
        "temperature": _TEMPERATURE,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
    }


_payload = request_body  # old private name, kept for callers that have not migrated


def template() -> dict:
    """The local prompt template, minus questions and text, for providers.prompt_template_hash."""
    return {
        "instructions": _INSTRUCTIONS,
        "retry": _RETRY_NOTE,
        "schema": _FEATURE_SCHEMA,
        "temperature": _TEMPERATURE,
        "max_tokens": MAX_OUTPUT_TOKENS,
    }


def extract(
    model: str,
    current: str,
    previous: str,
    questions: dict,
    *,
    base_url: str,
    transport: Transport | None = None,
    timeout: float = 120.0,
) -> dict:
    """Extract bounded features with a local model; same result shape as the paid providers.

    ``transport(url, payload, api_key, timeout) -> dict`` matches ``providers.Transport``;
    it receives an empty key. A malformed answer is retried exactly once with a short
    correction appended; a second one raises ``ProviderValidationError``. Transport, HTTP,
    refusal and engine errors raise ``ProviderError`` and are never retried.
    """
    _inputs(model, current, previous, questions)
    try:
        wait = _timeout(timeout)
    except ProviderValidationError as exc:
        raise ProviderInputError(str(exc)) from None
    url = f"{normalize_base_url(base_url)}/chat/completions"
    problem = ""
    for attempt in range(2):
        request = request_body(model, current, previous, questions)
        if attempt:
            request["messages"].append({"role": "user", "content": _RETRY_NOTE})
        try:
            raw = _call(transport, url, request, wait)
            features, resolved, tokens = _completion(raw)
            return _result(features, raw, resolved, tokens, bool(previous.strip()))
        except ProviderInputError:
            raise
        except ProviderValidationError as exc:
            problem = str(exc)
    raise ProviderValidationError(f"Local model output was invalid after one retry: {problem}")


def _served(listing: dict) -> list[str] | None:
    data = listing.get("data")
    if not isinstance(data, list):
        return None
    return [
        item["id"]
        for item in data
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item["id"].isprintable()
        and 0 < len(item["id"]) <= 200
    ]


def health_check(
    base_url: str, model: str, *, transport: Transport | None = None, timeout: float = 30.0
) -> dict:
    """Report whether the engine answers, serves ``model`` and returns valid structured output.

    Lists ``GET {base_url}/models`` (payload None through the transport), then runs one
    tiny structured probe through ``extract``. Only invalid arguments raise
    (``ProviderInputError``); every engine problem is reported in the result.
    """
    base = normalize_base_url(base_url)
    try:
        _model(model)
        wait = _timeout(timeout)
    except ProviderValidationError as exc:
        raise ProviderInputError(str(exc)) from None
    report: dict = {
        "ok": False,
        "reachable": False,
        "model_present": False,
        "structured_output_ok": False,
        "latency_ms": None,
        "detail": "",
    }
    try:
        listing = _call(transport, f"{base}/models", None, wait)
    except EngineUnreachable as exc:
        return {**report, "detail": f"{exc} ({base}); start the engine or check local_base_url"}
    except ProviderError as exc:
        return {
            **report,
            "reachable": True,
            "detail": f"{base}/models failed: {exc}; local_base_url usually ends with /v1",
        }
    report["reachable"] = True
    served = _served(listing)
    if served is None:
        return {**report, "detail": f"{base}/models is not an OpenAI-compatible model list"}
    if model not in served and (":" in model or f"{model}:latest" not in served):
        listed = ", ".join(served[:5]) + (", ..." if len(served) > 5 else "")
        return {
            **report,
            "detail": f"Model {model} is not served at {base}; pull or load it first"
            + (f" (served: {listed})" if listed else ""),
        }
    report["model_present"] = True
    started = time.monotonic()
    try:
        extract(
            model,
            _PROBE_CURRENT,
            _PROBE_PREVIOUS,
            _PROBE_QUESTIONS,
            base_url=base,
            transport=transport,
            timeout=wait,
        )
    except ProviderValidationError as exc:
        detail = f"{model} answered, but not with valid structured output: {exc}"
    except ProviderError as exc:
        detail = f"Structured probe to {model} failed: {exc}"
    else:
        report.update(ok=True, structured_output_ok=True)
        detail = ""
    report["latency_ms"] = max(0, round((time.monotonic() - started) * 1000))
    report["detail"] = detail or f"{model} answered a structured probe in {report['latency_ms']} ms"
    return report


# Private aliases kept until every caller patches the public seams (P0-26, #30).
# Patching an alias does not change what the module calls.
_http_json = http_json
_base_url = normalize_base_url
