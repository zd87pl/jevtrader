"""Canary secrets (Phase 1 A7 DoD, #28): no key value leaves the process by any output channel.

Every known secret name gets a distinct canary value in the environment and another in an
injected fake secret store. Setup, doctor, the service files, the daemon jobs, the brief, every
MCP tool, every web route and a provider and an SEC failure then run against fakes, and every
output channel is searched for each canary: logging, stdout and stderr, what setup says, the
config file, the ledger bytes, the plist and systemd unit, run records, reports and every argv
handed to an injected runner. The fakes also record that the keys reached the code that needs
them, so the absence is not vacuous.
"""

import contextlib
import functools
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jevtrader import (
    app,
    bars,
    brief,
    daemon,
    feeds,
    mcp_server,
    paths,
    pipeline,
    providers,
    sec,
    secrets,
    web,
)
from jevtrader import config as settings
from jevtrader.common import load_strategy
from jevtrader.store import Ledger
from tests.test_app import FakeKeychain, FakeLaunchctl
from tests.test_e2e import EVENT_ID, FIRST, UA, Clock, FakeAlpaca, FakeSEC
from tests.test_providers import openai_response

# Distinct per name and per channel; each matches the secret stores' value grammar.
ENV_CANARIES = {name: f"cnryEnv{i}Q7z{name[:4]}x91k" for i, name in enumerate(secrets.KNOWN)}
STORE_CANARIES = {name: f"cnryStore{i}W3v{name[:4]}p58d" for i, name in enumerate(secrets.KNOWN)}
CANARIES = [*ENV_CANARIES.values(), *STORE_CANARIES.values()]
# Setup: SEC contact, no watchlist, every 8-K, OpenAI gpt-x with a cap and both prices,
# Alpaca bars, default brief time and notification, then start the service.
ANSWERS = [UA, "none", "all", "4", "gpt-x", "12", "2.5", "10", "y", "", "", "y"]


class Recorder:
    """A runner for systemctl: records argv and answers success (inactive for is-active)."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        code = 3 if "is-active" in argv else 0
        return SimpleNamespace(returncode=code, stdout="", stderr="")


class Alpaca(FakeAlpaca):
    """FakeAlpaca that also records the secret header, proving both keys were sent."""

    def __init__(self):
        super().__init__()
        self.secrets: set[str] = set()

    def __call__(self, url, headers, timeout):
        self.secrets.add(headers["APCA-API-SECRET-KEY"])
        return super().__call__(url, headers, timeout)


class OpenAI:
    """A provider transport: records the key it was given; fails with it in the text if asked."""

    def __init__(self, *, fail=False):
        self.keys: list[str] = []
        self.fail = fail

    def __call__(self, url, payload, key, timeout):
        self.keys.append(key)
        if self.fail:
            raise RuntimeError(f"401 from {url}; Authorization: Bearer {key}")
        return openai_response()


class LeakySEC(FakeSEC):
    """SEC transport whose failure text carries every key the process holds, as a careless
    proxy echoing its environment might."""

    def __call__(self, request, *, timeout):
        raise OSError("proxy said: " + " ".join(os.environ.get(n, "") for n in secrets.KNOWN))


class CanarySecretTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.clock = Clock(FIRST)
        self.texts: list[str] = []  # every captured output, searched at the end
        self.argvs: list[list[str]] = []
        quiet = sec._RateLimiter(lambda: 0.0, lambda _: None)
        env = {paths.HOME_ENV: str(self.dir / "home")}
        for patcher in (
            patch.dict(os.environ, env),
            patch.object(secrets, "keychain_available", return_value=False),
            patch.object(sec, "_DEFAULT_LIMITER", quiet),
            patch.object(bars, "_LIMITER", bars._RateLimiter(lambda: 0.0, lambda _: None)),
            patch.object(sec, "utc_now", self.clock.moment),
            patch("jevtrader.engine.utc_now", self.clock),
            patch("jevtrader.market.utc_now", self.clock),
            patch("jevtrader.bars.utc_now", self.clock),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in secrets.KNOWN:
            os.environ.pop(name, None)
        self.logs = io.StringIO()
        handler = logging.StreamHandler(self.logs)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous)
        self.addCleanup(root.removeHandler, handler)

    # ------------------------------------------------------------ stages

    def run_setup(self, keychain: FakeKeychain, launchctl: FakeLaunchctl) -> dict:
        replies = iter(ANSWERS)
        typed = iter(
            STORE_CANARIES[name]
            for name in ("OPENAI_API_KEY", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
        )
        said: list[str] = []
        result = app.setup(
            ask=lambda prompt: next(replies),
            ask_secret=lambda prompt: next(typed),
            say=said.append,
            keychain_runner=keychain,
            launchd_runner=launchctl,
            program=["/usr/bin/python3", "-m", "jevtrader", "daemon"],
            home=self.dir,
        )
        self.texts += said + [json.dumps(result)]
        return result

    def run_jobs(self, config: dict) -> tuple[Alpaca, OpenAI, OpenAI, list[str]]:
        alpaca, failing, working, lines = Alpaca(), OpenAI(fail=True), OpenAI(), []
        fake_sec = FakeSEC()
        strategy = load_strategy()
        options = daemon.observe_options(config)
        with Ledger(str(settings.ledger_path(config)), create=False) as ledger:

            def context(poll_transport, observe_transport, **extra):
                return app.daemon_context(
                    ledger,
                    config,
                    strategy,
                    clock=self.clock,
                    poll=lambda *a, **k: feeds.poll(
                        *a, **k, transport=poll_transport, memory=feeds.PollMemory()
                    ),
                    bars=lambda *a, **k: bars.fetch_forward(*a, **k, transport=alpaca),
                    observe=functools.partial(
                        pipeline.observe_queue, **options, transport=observe_transport, **extra
                    ),
                    notify=lambda title, body: lines.append(title + body) or True,
                    log=lines.append,
                )

            records = [daemon.run_job("poll", context(LeakySEC(), working))]  # SEC failure
            ctx = context(fake_sec, failing)
            records += [daemon.run_job(job, ctx) for job in ("poll", "bars", "observe")]
            ctx = context(fake_sec, working, retry_failed=True)
            records += [daemon.run_job(job, ctx) for job in ("observe", "settle", "brief")]
        self.texts += lines + [json.dumps(records)]
        statuses = [(record["job"], record["status"]) for record in records]
        self.assertEqual(statuses[0][0], "poll")
        self.assertNotEqual(statuses[0][1], "ok")  # the SEC failure was recorded
        self.assertEqual(
            statuses[1:],
            [
                ("poll", "ok"),
                ("bars", "ok"),
                ("observe", "partial"),  # the provider failure was recorded
                ("observe", "ok"),
                ("settle", "ok"),
                ("brief", "ok"),
            ],
            records,
        )
        return alpaca, failing, working, lines

    def run_readers(self, config: dict) -> None:
        ledger_path = str(settings.ledger_path(config))
        report = app.compose_brief(ledger_path, config, now=self.clock.now)
        title, body = brief.render_text(report)
        self.texts += [json.dumps(report), title, body, brief.render_html(report)]
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ]
        arguments = {"explain_filing": {"event_id": EVENT_ID}, "search_filings": {"symbol": "abc"}}
        for number, tool in enumerate(mcp_server.TOOLS, 2):
            name = tool["name"]
            params = {"name": name, "arguments": arguments.get(name, {})}
            messages.append(
                {"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": params}
            )
        stdout, stderr = io.StringIO(), io.StringIO()
        mcp_server.serve(
            app.mcp_handlers(ledger_path, config, clock=self.clock),
            stdin=io.StringIO("".join(json.dumps(m) + "\n" for m in messages)),
            stdout=stdout,
            stderr=stderr,
        )
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        for reply in replies[2:]:
            self.assertNotIn("isError", reply["result"], reply)
        self.assertEqual(len(replies), len(mcp_server.TOOLS) + 2)
        self.texts += [stdout.getvalue(), stderr.getvalue()]
        routes = [
            "/",
            "/api/brief.json",
            "/api/scoreboard.json",
            "/scoreboard",
            "/health",
            "/filing/" + EVENT_ID.replace(":", "%3A"),
            "/static/app.css",
            "/missing",
        ]
        for route in routes:
            status, headers, page = web.respond(
                ledger_path, "GET", route, ["127.0.0.1:8765"], port=8765, now=self.clock.now
            )
            self.assertIn(status, (200, 404), route)
            self.texts += [json.dumps(headers), page.decode()]

    def provider_failure(self) -> list[str]:
        seen: list[str] = []

        def transport(url, payload, key, timeout):
            seen.append(key)
            raise RuntimeError(f"Authorization: Bearer {key}")

        for provider in ("jev", "openai"):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.extract_features(
                    provider, "m", "text", "", load_strategy(), transport=transport
                )
            self.texts.append(f"{caught.exception!r} {caught.exception}")
        return seen

    # ------------------------------------------------------------ the test

    def test_no_canary_reaches_any_output(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        keychain, launchctl, systemctl = FakeKeychain(), FakeLaunchctl(), Recorder()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            # Setup stores the typed keys in the fake Keychain, on stdin only.
            result = self.run_setup(keychain, launchctl)
            self.assertEqual(
                result["keys_stored"],
                ["OPENAI_API_KEY", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"],
            )
            # Doctor loads them from the store into the environment it reports on.
            report = app.doctor(launchd_runner=launchctl, keychain_runner=keychain, home=self.dir)
            self.texts.append(json.dumps(report))
            self.assertTrue(report["checks"]["keys"]["ok"], report)
            for name in result["keys_stored"]:
                self.assertEqual(os.environ[name], STORE_CANARIES[name])
            # A fake store object is read the same way (the non-runner seam).
            store = SimpleNamespace(get=STORE_CANARIES.get, set=None, delete=None)
            os.environ.pop("TYPESAFE_API_KEY", None)
            self.assertEqual(
                secrets.export_to_environ(["TYPESAFE_API_KEY"], store=store),
                ["TYPESAFE_API_KEY"],
            )
            # From here the environment canaries win, as a service override would.
            os.environ.update(ENV_CANARIES)
            config = settings.load()
            program = app.program_args()
            for platform, runner in (("darwin", launchctl), ("linux", systemctl)):
                service = app.up(
                    config,
                    ledger_path=str(settings.ledger_path(config)),
                    program=program,
                    runner=runner,
                    home=self.dir,
                    platform=platform,
                )
                self.texts.append(json.dumps(service))
            alpaca, failing, working, _ = self.run_jobs(config)
            self.run_readers(config)
            provider_keys = self.provider_failure()

        # The keys really were available to the code that needs them.
        self.assertEqual(alpaca.keys, {ENV_CANARIES["ALPACA_API_KEY_ID"]})
        self.assertEqual(alpaca.secrets, {ENV_CANARIES["ALPACA_API_SECRET_KEY"]})
        self.assertEqual(set(failing.keys), {ENV_CANARIES["OPENAI_API_KEY"]})
        self.assertEqual(set(working.keys), {ENV_CANARIES["OPENAI_API_KEY"]})
        self.assertEqual(
            provider_keys, [ENV_CANARIES["TYPESAFE_API_KEY"], ENV_CANARIES["OPENAI_API_KEY"]]
        )
        stored = {name: keychain.items[name] for name in result["keys_stored"]}
        self.assertEqual(stored, {name: STORE_CANARIES[name] for name in stored})

        # Every argv handed to an injected runner (stdin is the designed channel for values).
        self.argvs += [argv for argv, _ in keychain.calls] + launchctl.calls + systemctl.calls
        self.assertTrue(launchctl.calls and systemctl.calls and keychain.calls)
        written = sorted(p for p in self.dir.rglob("*") if p.is_file())
        names = {p.name for p in written}
        self.assertIn(paths.config_path().name, names)
        self.assertTrue(any(p.suffix == ".plist" for p in written), names)
        self.assertTrue(any(p.suffix == ".service" for p in written), names)
        self.assertTrue(any(".sqlite" in p.name for p in written), names)
        channels = {
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "logging": self.logs.getvalue(),
            "argv": json.dumps(self.argvs),
            "outputs": "\n".join(self.texts),
        }
        channels.update({str(p): p.read_bytes().decode("latin-1") for p in written})
        for canary in CANARIES:
            for channel, text in channels.items():
                self.assertNotIn(canary, text, f"a canary leaked into {channel}")


if __name__ == "__main__":
    unittest.main()
