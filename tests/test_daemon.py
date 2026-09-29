"""The daemon schedule, job records, spend cap and loop; every adapter is a fake, nothing leaves."""

import contextlib
import copy
import fcntl
import json
import os
import stat
import tempfile
import threading
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from jevtrader import config as settings
from jevtrader import bars, brief, daemon, feeds, notify, secrets
from jevtrader.common import EASTERN as ET
from jevtrader.common import canonical, load_strategy, timestamp
from jevtrader.daemon import Context, due_jobs, run_job
from jevtrader.store import Ledger

UA = "Research Person research@example.com"
STRATEGY = load_strategy()


def et(day, hour=0, minute=0, second=0):
    """An America/New_York wall-clock time on an ISO date."""
    return datetime.combine(datetime.fromisoformat(day).date(), time(hour, minute, second), ET)


def iso(moment):
    return timestamp(moment.isoformat())


def conf(**changes):
    base = {"sec_user_agent": UA, "watchlist": ["ABC", "XYZ"], "bars_source": "alpaca"}
    return settings.validate({**base, **changes})


def state(attempted=None, completed=None, pending=False):
    return {
        "attempted": {job: iso(value) for job, value in (attempted or {}).items()},
        "completed": {job: iso(value) for job, value in (completed or {}).items()},
        "pending": {"observe": pending},
    }


class FakeLedger:
    """Only the calls the daemon makes: immutable put, all and prefix (sorted by id)."""

    def __init__(self):
        self.records = {}

    def put(self, kind, identity, payload):
        encoded = canonical(payload)  # the real ledger rejects non-JSON and NaN
        if (kind, identity) in self.records:
            if canonical(self.records[kind, identity]) != encoded:
                raise ValueError(f"Immutable record conflict: {kind}/{identity}")
            return False
        self.records[kind, identity] = json.loads(encoded)
        return True

    def get(self, kind, identity):
        return copy.deepcopy(self.records.get((kind, identity)))

    def all(self, kind):
        return [copy.deepcopy(v) for (k, _), v in sorted(self.records.items()) if k == kind]

    def prefix(self, kind, prefix):
        return [
            copy.deepcopy(v)
            for (k, i), v in sorted(self.records.items())
            if k == kind and i.startswith(prefix)
        ]


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return iso(self.now)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Fakes:
    """Adapters with call logs and contract-shaped results."""

    def __init__(self):
        self.calls = []
        self.poll_result = {
            "seen": 5,
            "new": 1,
            "added": ["sec:0000000001-26-000001:ex99.htm"],
            "skipped": {"unmapped": 2, "filtered": 2},
            "errors": [],
        }
        self.observe_result = {
            "forecasts": [{"id": "f1"}],
            "errors": [],
            "skipped": {"requires_replay": 0, "no_market_data": 2, "missing_credentials": False},
        }
        self.brief_result = {"filings": [{"id": "a"}, {"id": "b"}]}
        self.reconcile_result = {"indexed": 0, "gaps": [], "errors": []}
        self.notify_result = True
        self.benchmarks = []
        self.out_of_time = []
        self.calibrators = []

    def poll(self, ledger, user_agent, *, symbols):
        self.calls.append(("poll", user_agent, symbols))
        return self.poll_result

    def bars(self, ledger, symbols, *, now, feed, benchmark):
        self.calls.append(("bars", list(symbols), now, feed))
        self.benchmarks.append(benchmark)
        return {"symbols": len(symbols) + 1, "added": 4, "skipped": 1, "errors": []}

    def observe(self, ledger, strategy, *, provider, model, calibrator, limit, out_of_time):
        self.calls.append(("observe", provider, model, limit))
        self.calibrators.append(calibrator)
        self.out_of_time.append(out_of_time)
        return self.observe_result

    def settle(self, ledger, *, as_of):
        self.calls.append(("settle", as_of))
        return {"added": 2, "unresolved_count": 7, "unresolved_ids": ["x"] * 7}

    def brief(self, ledger, *, now, since, watchlist):
        self.calls.append(("brief", now, since, watchlist))
        return self.brief_result

    def render(self, report):
        self.calls.append(("render", len(report["filings"])))
        return "Brief", "2 filings"

    def notify(self, title, body):
        self.calls.append(("notify", title, body))
        if isinstance(self.notify_result, BaseException):
            raise self.notify_result
        return self.notify_result

    def reconcile(self, ledger, user_agent, day, *, symbols):
        self.calls.append(("reconcile", user_agent, day, symbols))
        return self.reconcile_result

    def jobs(self):
        return [call[0] for call in self.calls]


def context(ledger=None, clock=None, fakes=None, config=None, price=None, **changes):
    fakes = fakes or Fakes()
    return Context(
        ledger=ledger if ledger is not None else FakeLedger(),
        config=config or conf(),
        strategy=changes.pop("strategy", STRATEGY),
        clock=clock or Clock(et("2026-09-28", 9, 30)),
        poll=fakes.poll,
        bars=fakes.bars,
        observe=fakes.observe,
        settle=fakes.settle,
        brief=fakes.brief,
        render=fakes.render,
        notify=fakes.notify,
        reconcile=fakes.reconcile,
        price=price or (lambda provider, model: None),
        log=changes.pop("log", lambda line: None),
        **changes,
    )


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)
        environment = patch.dict(os.environ, {"JEVTRADER_HOME": str(self.dir / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        for name in secrets.KNOWN:
            os.environ.pop(name, None)


class ScheduleTests(unittest.TestCase):
    def test_poll_every_minute_on_weekdays_and_fifteen_minutes_otherwise(self):
        cases = [
            (et("2026-09-28", 9, 30), 59, False),
            (et("2026-09-28", 9, 30), 60, True),
            (et("2026-09-28", 6, 0), 60, True),  # 06:00 is inside the active window
            (et("2026-09-28", 5, 59, 59), 60, False),
            (et("2026-09-28", 5, 59, 59), 900, True),
            (et("2026-09-28", 21, 59, 59), 60, True),
            (et("2026-09-28", 22, 0), 60, False),  # 22:00 is outside it
            (et("2026-09-28", 22, 0), 899, False),
            (et("2026-09-28", 22, 0), 900, True),
            (et("2026-09-26", 12, 0), 60, False),  # Saturday
            (et("2026-09-26", 12, 0), 900, True),
        ]
        for now, elapsed, due in cases:
            with self.subTest(now=now, elapsed=elapsed):
                current = state({"poll": now - timedelta(seconds=elapsed)})
                self.assertEqual("poll" in due_jobs(now, current, conf()), due)
        self.assertEqual(daemon.poll_interval(et("2026-09-28", 12)), 60)
        self.assertEqual(daemon.poll_interval(et("2026-09-27", 12)), 900)

    def test_first_start_catches_up_every_slot_in_job_order(self):
        now = et("2026-09-28", 17, 0)
        self.assertEqual(due_jobs(now, {}, conf()), ["poll", "bars", "brief", "reconcile"])
        # Settle waits for this slot's bars; without a bar source it follows the slot alone.
        self.assertEqual(
            due_jobs(now, {}, conf(bars_source="none")), ["poll", "settle", "brief", "reconcile"]
        )

    def test_bars_once_after_1630_on_weekdays_with_catch_up(self):
        monday_done = {"bars": et("2026-09-28", 16, 31)}
        cases = [
            (et("2026-09-29", 16, 29, 59), monday_done, False),
            (et("2026-09-29", 16, 30), monday_done, True),
            (et("2026-09-28", 16, 40), monday_done, False),
            # Asleep all Friday afternoon: Monday morning catches up Friday's session.
            (et("2026-09-28", 9, 0), {"bars": et("2026-09-24", 16, 40)}, True),
            (et("2026-09-26", 12, 0), {"bars": et("2026-09-25", 16, 31)}, False),
            (et("2026-09-27", 23, 0), {"bars": et("2026-09-24", 16, 31)}, True),
        ]
        for now, completed, due in cases:
            with self.subTest(now=now):
                current = state(completed, completed)
                self.assertEqual("bars" in due_jobs(now, current, conf()), due)
        self.assertNotIn("bars", due_jobs(et("2026-09-29", 17), {}, conf(bars_source="none")))

    def test_failed_slot_job_retries_after_backoff_only(self):
        current = state({"bars": et("2026-09-28", 16, 31)}, {"bars": et("2026-09-25", 16, 31)})
        self.assertNotIn("bars", due_jobs(et("2026-09-28", 16, 45), current, conf()))
        self.assertIn("bars", due_jobs(et("2026-09-28", 16, 46), current, conf()))

    def test_settle_follows_the_slots_bars(self):
        now = et("2026-09-28", 16, 40)
        before = state({"bars": et("2026-09-25", 16, 31)}, {"bars": et("2026-09-25", 16, 31)})
        self.assertNotIn("settle", due_jobs(now, before, conf()))
        after = state({"bars": et("2026-09-28", 16, 31)}, {"bars": et("2026-09-28", 16, 31)})
        self.assertIn("settle", due_jobs(now, after, conf()))
        after["completed"]["settle"] = iso(et("2026-09-28", 16, 32))
        after["attempted"]["settle"] = after["completed"]["settle"]
        self.assertNotIn("settle", due_jobs(now, after, conf()))
        failed_bars = state({"bars": et("2026-09-28", 16, 31)}, {"bars": et("2026-09-25", 16, 31)})
        self.assertNotIn("settle", due_jobs(now, failed_bars, conf()))

    def test_brief_once_per_weekday_at_brief_time_without_cross_day_catch_up(self):
        monday = {"brief": et("2026-09-28", 8, 45)}
        cases = [
            (et("2026-09-29", 8, 44, 59), monday, False, "08:45"),
            (et("2026-09-29", 8, 45), monday, True, "08:45"),
            (et("2026-09-28", 12, 0), monday, False, "08:45"),
            (et("2026-09-26", 9, 0), {}, False, "08:45"),  # Saturday
            (et("2026-09-29", 7, 0), {"brief": et("2026-09-25", 8, 45)}, False, "08:45"),
            (et("2026-09-29", 7, 30), monday, True, "07:30"),
            (et("2026-09-29", 7, 29), monday, False, "07:30"),
        ]
        for now, completed, due, brief_time in cases:
            with self.subTest(now=now, brief_time=brief_time):
                current = state(completed, completed)
                result = due_jobs(now, current, conf(brief_time=brief_time))
                self.assertEqual("brief" in result, due)

    def test_brief_time_is_eastern_across_daylight_saving(self):
        current = state({"brief": et("2026-03-06", 8, 45)}, {"brief": et("2026-03-06", 8, 45)})
        summer = datetime(2026, 3, 9, 12, 45, tzinfo=timezone.utc)  # 08:45 EDT
        self.assertIn("brief", due_jobs(summer, current, conf()))
        self.assertNotIn("brief", due_jobs(summer - timedelta(minutes=1), current, conf()))
        current = state({"brief": et("2026-01-02", 8, 45)}, {"brief": et("2026-01-02", 8, 45)})
        winter = datetime(2026, 1, 5, 13, 45, tzinfo=timezone.utc)  # 08:45 EST
        self.assertIn("brief", due_jobs(winter, current, conf()))
        self.assertNotIn("brief", due_jobs(winter - timedelta(minutes=1), current, conf()))

    def test_reconcile_at_2245_with_catch_up(self):
        monday = {"reconcile": et("2026-09-28", 22, 46)}
        self.assertNotIn(
            "reconcile", due_jobs(et("2026-09-29", 22, 44), state(monday, monday), conf())
        )
        self.assertIn(
            "reconcile", due_jobs(et("2026-09-29", 22, 45), state(monday, monday), conf())
        )
        thursday = {"reconcile": et("2026-10-01", 22, 50)}
        saturday = et("2026-10-03", 10, 0)
        self.assertIn("reconcile", due_jobs(saturday, state(thursday, thursday), conf()))
        self.assertEqual(daemon.reconcile_day(saturday).isoformat(), "2026-10-02")
        self.assertEqual(daemon.reconcile_day(et("2026-09-29", 22, 44)).isoformat(), "2026-09-28")
        self.assertEqual(daemon.reconcile_day(et("2026-09-29", 22, 45)).isoformat(), "2026-09-29")

    def test_observe_needs_pending_work_and_a_minute_between_runs(self):
        now = et("2026-09-28", 9, 30)
        self.assertNotIn("observe", due_jobs(now, state(pending=False), conf()))
        self.assertIn("observe", due_jobs(now, state(pending=True), conf()))
        recent = state({"observe": now - timedelta(seconds=30)}, pending=True)
        self.assertNotIn("observe", due_jobs(now, recent, conf()))
        recent = state({"observe": now - timedelta(seconds=60)}, pending=True)
        self.assertIn("observe", due_jobs(now, recent, conf()))

    def test_poll_backs_off_after_a_failure_or_an_sec_stop(self):
        now = et("2026-09-28", 9, 30)
        current = state({"poll": now - timedelta(seconds=120)})
        current["backoff"] = {"poll": True}
        self.assertNotIn("poll", due_jobs(now, current, conf()))
        current["attempted"]["poll"] = iso(now - timedelta(seconds=daemon.RETRY_SECONDS))
        self.assertIn("poll", due_jobs(now, current, conf()))
        memory = {}
        for status, counts, backoff in (
            ("failed", {}, True),
            ("partial", {"added": 0, "stopped": 1}, True),
            ("partial", {"added": 0, "stopped": 0}, False),
            ("ok", {"added": 1}, False),
        ):
            with self.subTest(status=status, counts=counts):
                record = {"job": "poll", "status": status, "started_at": "t", "counts": counts}
                daemon.remember(memory, record)
                self.assertEqual(memory["backoff"]["poll"], backoff)

    def test_a_clock_moved_backwards_does_not_stall_polling(self):
        now = et("2026-09-28", 9, 30)
        self.assertIn("poll", due_jobs(now, state({"poll": now + timedelta(hours=2)}), conf()))

    def test_naive_times_and_bad_brief_times_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            due_jobs(datetime(2026, 9, 28, 9, 30), {}, conf())
        with self.assertRaisesRegex(ValueError, "brief_time"):
            due_jobs(et("2026-09-28", 9, 30), {}, {"brief_time": "25:00"})


class RunJobTests(IsolatedTest):
    def test_run_records_an_immutable_runs_entry(self):
        clock = Clock(et("2026-09-28", 9, 30))
        ledger, fakes = FakeLedger(), Fakes()
        record = run_job("poll", context(ledger, clock, fakes))
        self.assertEqual(
            {k: v for k, v in record.items() if k != "id"},
            {
                "job": "poll",
                "started_at": "2026-09-28T13:30:00.000000Z",
                "finished_at": "2026-09-28T13:30:00.000000Z",
                "status": "ok",
                "counts": {
                    "seen": 5,
                    "new": 1,
                    "added": 1,
                    "skipped": 4,
                    "errors": 0,
                    "requests": 0,
                    "stopped": 0,
                },
                "error": None,
            },
        )
        self.assertTrue(record["id"].startswith("poll:2026-09-28T13:30:00.000000Z:"))
        self.assertEqual(ledger.get("runs", record["id"]), record)
        self.assertEqual(fakes.calls, [("poll", UA, {"ABC", "XYZ"})])

    def test_real_ledger_accepts_run_records(self):
        with Ledger(":memory:") as ledger:
            record = run_job("settle", context(ledger))
            self.assertEqual(ledger.get("runs", record["id"]), record)
            self.assertEqual(daemon.last_run(ledger, "settle"), record)
            self.assertEqual(
                daemon.load_state(ledger)["completed"], {"settle": record["started_at"]}
            )

    def test_poll_scope_and_skips(self):
        fakes = Fakes()
        run_job("poll", context(fakes=fakes, config=conf(universe="all")))
        self.assertEqual(fakes.calls, [("poll", UA, None)])
        for config, reason in (
            (conf(sec_user_agent=""), "sec_user_agent is not set"),
            (conf(watchlist=[]), "watchlist is empty"),
        ):
            with self.subTest(reason=reason):
                fakes = Fakes()
                record = run_job("poll", context(fakes=fakes, config=config))
                self.assertEqual((record["status"], record["counts"]), ("skipped", {}))
                self.assertIn(reason, record["error"])
                self.assertEqual(fakes.calls, [])

    def test_reported_errors_make_a_partial_run(self):
        fakes = Fakes()
        fakes.poll_result = {**fakes.poll_result, "errors": [{"accession": "a", "error": "boom"}]}
        record = run_job("poll", context(fakes=fakes))
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["error"], "1 error(s); first: boom")
        self.assertEqual(record["counts"]["errors"], 1)

    def test_exceptions_are_recorded_without_secrets_or_control_characters(self):
        os.environ["OPENAI_API_KEY"] = "sk-proj-SECRET123"
        fakes = Fakes()

        def explode(*args, **kwargs):
            raise RuntimeError(
                "sent sk-proj-SECRET123 with Authorization: Bearer abc.def\x1b[31m and "
                "APCA-API-SECRET-KEY: s3cr3t " + "x" * 1000
            )

        fakes.poll = explode
        lines = []
        record = run_job("poll", context(fakes=fakes, log=lines.append))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(len(lines), 1)
        self.assertIn("poll failed {}", lines[0])
        self.assertNotIn("SECRET123", lines[0])
        self.assertNotIn("SECRET123", record["error"])
        self.assertNotIn("abc.def", record["error"])
        self.assertNotIn("s3cr3t", record["error"])
        self.assertNotIn("\x1b", record["error"])
        self.assertTrue(record["error"].startswith("RuntimeError: sent [redacted]"))
        self.assertLessEqual(len(record["error"]), daemon.MAX_ERROR_CHARS)

    def test_unknown_job_is_rejected_without_a_record(self):
        ledger = FakeLedger()
        with self.assertRaisesRegex(ValueError, "Unknown job"):
            run_job("trade", context(ledger))
        self.assertEqual(ledger.records, {})

    def test_context_validates_config_and_strategy(self):
        with self.assertRaises(ValueError):
            context(config={"provider": "broker"})
        with self.assertRaises(ValueError):
            Context(ledger=FakeLedger(), config=conf(), strategy={"version": 1})

    def test_default_adapters_are_the_contract_functions(self):
        ctx = Context(ledger=FakeLedger(), config=conf(), strategy=STRATEGY)
        expected = {
            "poll": feeds.poll,
            "bars": bars.fetch_forward,
            # observe is bound to config (P0-25); see the construction-path test below.
            "settle": daemon.engine.settle,
            "brief": brief.compose,
            "render": brief.render_text,
            "notify": notify.macos,
            "reconcile": feeds.reconcile,
            "price": daemon.registry_price,
        }
        for field, target in expected.items():
            with self.subTest(field=field):
                self.assertIs(getattr(ctx, field), target)

    def test_every_construction_path_wires_observe_from_config(self):
        # P0-25: a bare Context must not silently use the default local URL or drop
        # the declared model overrides; both paths bind them from the validated config.
        from jevtrader import app

        declared = {"openai:gpt-x": {"usd_per_million_input_tokens": 2.5}}
        cases = {
            "local": (
                {"provider": "local", "local_base_url": "http://[::1]:1234/v1"},
                "http://[::1]:1234/v1",
            ),
            "paid": ({"provider": "openai", "model": "gpt-x", "model_overrides": declared}, None),
        }
        builders = {
            "Context": lambda config: Context(
                ledger=FakeLedger(), config=config, strategy=STRATEGY
            ),
            "daemon_context": lambda config: app.daemon_context(FakeLedger(), config, STRATEGY),
        }
        for name, build in builders.items():
            for case, (config, base_url) in cases.items():
                with self.subTest(path=name, case=case):
                    ctx = build(conf(**config))
                    self.assertIs(ctx.observe.func, daemon.pipeline.observe_queue)
                    self.assertEqual(ctx.observe.keywords["base_url"], base_url)
                    self.assertEqual(
                        ctx.observe.keywords["overrides"], ctx.config["model_overrides"]
                    )
        # An injected adapter still wins.
        fake = Fakes().observe
        ctx = Context(ledger=FakeLedger(), config=conf(), strategy=STRATEGY, observe=fake)
        self.assertIs(ctx.observe, fake)

    def test_brief_counts_all_new_filings_not_only_those_displayed(self):
        fakes = Fakes()
        fakes.brief_result = {"filings": [{"id": "a"}, {"id": "b"}], "total": 40}
        record = run_job("brief", context(fakes=fakes, config=conf(notify=False)))
        self.assertEqual(record["counts"]["filings"], 40)

    def test_bars_symbols_settle_open_forecasts_then_watchlist_then_recent_filings(self):
        ledger, fakes = FakeLedger(), Fakes()
        now = et("2026-09-28", 16, 30)
        for identity, symbol, mode, seen in (
            ("a", "NEW", "forward", now - timedelta(days=1)),
            ("b", "OLD", "forward", now - timedelta(days=61)),
            ("c", "HIS", "historical", now - timedelta(days=1)),
            ("d", "ABC", "forward", now - timedelta(days=2)),
            ("e", "MID", "forward", now - timedelta(days=30)),
        ):
            ledger.put(
                "disclosures",
                identity,
                {"id": identity, "symbol": symbol, "mode": mode, "first_seen_at": iso(seen)},
            )

        def forecast(identity, symbol, age_days, *, benchmark="SPY", mode="forward"):
            record = {
                "id": identity,
                "symbol": symbol,
                "mode": mode,
                "decision_at": iso(now - timedelta(days=age_days)),
                "strategy": {**STRATEGY, "benchmark": benchmark},
            }
            ledger.put("forecasts", identity, record)

        # An open forward forecast needs its symbol and its own strategy's benchmark until
        # it settles, even when its filing fell out of the recent-filings window.
        forecast("f1", "LATE", 3)
        forecast("f2", "EARLY", 59, benchmark="QQQ")
        forecast("f3", "DONE", 5)
        ledger.put("outcomes", "f3", {"forecast_id": "f3"})
        forecast("f4", "REPLAY", 5, mode="historical")
        forecast("f5", "GONE", 61)
        strategy = {**STRATEGY, "benchmark": "IWM"}
        record = run_job("bars", context(ledger, Clock(now), fakes, strategy=strategy))
        symbols = ["IWM", "EARLY", "QQQ", "LATE", "SPY", "ABC", "XYZ", "NEW", "MID"]
        self.assertEqual(fakes.calls, [("bars", symbols, iso(now), "sip")])
        self.assertEqual(fakes.benchmarks, ["IWM"])  # the strategy's, not the SPY default
        self.assertEqual(record["counts"], {"symbols": 10, "added": 4, "skipped": 1, "errors": 0})
        fakes = Fakes()
        record = run_job("bars", context(fakes=fakes, config=conf(bars_source="none")))
        self.assertEqual(record["status"], "skipped")
        self.assertEqual(fakes.calls, [])

    def test_bars_window_covers_a_long_label_horizon(self):
        ledger, fakes = FakeLedger(), Fakes()
        now = et("2026-09-28", 16, 30)
        ledger.put(
            "forecasts",
            "f",
            {
                "id": "f",
                "symbol": "SLOW",
                "mode": "forward",
                "decision_at": iso(now - timedelta(days=95)),
                "strategy": STRATEGY,
            },
        )
        ledger.put(
            "disclosures",
            "d",
            {
                "id": "d",
                "symbol": "SEEN",
                "mode": "forward",
                "first_seen_at": iso(now - timedelta(days=95)),
            },
        )
        for horizon, expected in ((10, []), (60, ["SLOW", "SEEN"])):
            with self.subTest(horizon=horizon):
                fakes = Fakes()
                strategy = {**STRATEGY, "horizon_sessions": horizon}  # 60 sessions: 98 days
                run_job(
                    "bars",
                    context(
                        ledger, Clock(now), fakes, config=conf(watchlist=[]), strategy=strategy
                    ),
                )
                self.assertEqual(fakes.calls[0][1], ["SPY", *expected])

    def test_observe_starts_no_new_event_after_its_time_budget(self):
        # A slow local model must not hold up polling; the rest of the queue waits a tick.
        clock, seen = Clock(et("2026-09-28", 10, 0)), []

        def observe(ledger, strategy, *, out_of_time, **kwargs):
            seen.append(out_of_time())
            clock.advance(daemon.OBSERVE_BUDGET_SECONDS - 1)
            seen.append(out_of_time())
            clock.advance(1)
            seen.append(out_of_time())
            return {"forecasts": [{"id": "f"}], "errors": [], "skipped": {"out_of_time": True}}

        ctx = context(clock=clock)
        ctx.observe = observe
        record = run_job("observe", ctx)
        self.assertEqual(seen, [False, False, True])
        self.assertEqual((record["status"], record["counts"]["out_of_time"]), ("ok", 1))
        current = {}
        daemon.remember(current, record)
        self.assertTrue(current["pending"]["observe"])  # 1 of 10 done: more is queued

    def test_observe_uses_the_configured_calibrator(self):
        # Without one every service decision is WATCH and the evidence gate can never move.
        for calibrator in (None, "ridge-1"):
            with self.subTest(calibrator=calibrator):
                fakes = Fakes()
                run_job("observe", context(fakes=fakes, config=conf(calibrator=calibrator)))
                self.assertEqual(fakes.calibrators, [calibrator])

    def test_observe_with_free_providers_uses_the_default_limit(self):
        for provider, model in (("rules", "rules-v1"), ("local", "gpt-oss:120b")):
            with self.subTest(provider=provider):
                fakes = Fakes()
                record = run_job("observe", context(fakes=fakes, config=conf(provider=provider)))
                self.assertEqual(fakes.calls, [("observe", provider, model, daemon.OBSERVE_LIMIT)])
                self.assertEqual(
                    record["counts"],
                    {
                        "forecasts": 1,
                        "errors": 0,
                        "limit": 10,
                        "missing_credentials": 0,
                        "no_market_data": 2,
                        "requires_replay": 0,
                    },
                )

    def test_settle_and_brief_and_reconcile(self):
        clock = Clock(et("2026-09-28", 8, 45))
        ledger, fakes = FakeLedger(), Fakes()
        ctx = context(ledger, clock, fakes)
        settled = run_job("settle", ctx)
        self.assertEqual(settled["counts"], {"added": 2, "unresolved": 7})
        first = run_job("brief", ctx)
        clock.advance(86400)
        run_job("brief", ctx)
        self.assertEqual(
            fakes.calls,
            [
                ("settle", iso(et("2026-09-28", 8, 45))),
                ("brief", iso(et("2026-09-28", 8, 45)), None, ["ABC", "XYZ"]),
                ("render", 2),
                ("notify", "Brief", "2 filings"),
                ("brief", iso(et("2026-09-29", 8, 45)), first["started_at"], ["ABC", "XYZ"]),
                ("render", 2),
                ("notify", "Brief", "2 filings"),
            ],
        )
        self.assertEqual(first["counts"], {"filings": 2, "notified": 1})

    def test_brief_since_ignores_failed_briefs(self):
        ledger, fakes = FakeLedger(), Fakes()
        clock = Clock(et("2026-09-28", 8, 45))
        good = run_job("brief", context(ledger, clock, fakes))
        clock.advance(86400)
        fakes.brief = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        self.assertEqual(run_job("brief", context(ledger, clock, fakes))["status"], "failed")
        clock.advance(60)
        fakes2 = Fakes()
        run_job("brief", context(ledger, clock, fakes2))
        self.assertEqual(fakes2.calls[0][2], good["started_at"])

    def test_brief_notification_is_optional_and_never_fails_the_brief(self):
        fakes = Fakes()
        record = run_job("brief", context(fakes=fakes, config=conf(notify=False)))
        self.assertEqual(fakes.jobs(), ["brief"])
        self.assertEqual((record["status"], record["counts"]["notified"]), ("ok", 0))
        fakes = Fakes()
        fakes.notify_result = OSError("osascript missing")
        record = run_job("brief", context(fakes=fakes))
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["counts"], {"filings": 2, "notified": 0})
        self.assertIn("notification failed: OSError", record["error"])
        fakes = Fakes()
        fakes.notify_result = False  # not macOS
        self.assertEqual(run_job("brief", context(fakes=fakes))["counts"]["notified"], 0)

    def test_reconcile_counts_recoveries_and_records_filing_gaps(self):
        ledger, fakes = FakeLedger(), Fakes()
        fakes.reconcile_result = {
            "indexed": 5,
            "collected": 1,
            "recovered": ["sec:0000000002-26-000002:ex99.htm"],
            "not_qualifying": 1,
            "unmapped": 0,
            "not_watched": 1,
            "gaps": [
                {
                    "accession": "0000000003-26-000003",
                    "cik": "3",
                    "form": "8-K",
                    "reason": "not_in_submissions",
                },
            ],
            "index_missing": False,
            "errors": [],
            "stopped": None,
            "requests": 6,
        }
        clock = Clock(et("2026-09-29", 7, 0))  # catches up Monday's index
        record = run_job("reconcile", context(ledger, clock, fakes))
        self.assertEqual(fakes.calls[0][1:], (UA, datetime(2026, 9, 28).date(), {"ABC", "XYZ"}))
        self.assertEqual(record["status"], "ok")
        self.assertEqual(
            record["counts"],
            {
                "indexed": 5,
                "collected": 1,
                "recovered": 1,
                "not_qualifying": 1,
                "unmapped": 0,
                "not_watched": 1,
                "gaps": 1,
                "index_missing": 0,
                "requests": 6,
                "stopped": 0,
            },
        )
        (gap,) = ledger.prefix("runs", f"{daemon.FILING_GAP_JOB}:")
        self.assertEqual((gap["status"], gap["day"]), ("gap", "2026-09-28"))
        self.assertEqual(gap["gaps"][0]["reason"], "not_in_submissions")
        self.assertIn(daemon.FILING_GAP_JOB, daemon.last_runs(ledger))
        fakes = Fakes()
        record = run_job("reconcile", context(fakes=fakes, config=conf(sec_user_agent="")))
        self.assertEqual((record["status"], fakes.calls), ("skipped", []))

    def test_reconcile_missing_index_and_errors(self):
        ledger, fakes = FakeLedger(), Fakes()
        fakes.reconcile_result = {
            "index_missing": True,
            "gaps": [],
            "errors": [{"accession": "a", "error": "HTTPError: 429"}],
            "stopped": "HTTPError: 429",
        }
        record = run_job("reconcile", context(ledger, fakes=fakes))
        self.assertEqual(record["status"], "partial")
        self.assertEqual((record["counts"]["index_missing"], record["counts"]["stopped"]), (1, 1))
        (gap,) = ledger.prefix("runs", f"{daemon.FILING_GAP_JOB}:")
        self.assertIn("daily index missing", gap["error"])
        fakes.reconcile_result = {"indexed": 3, "collected": 3, "gaps": []}
        ledger = FakeLedger()
        self.assertEqual(run_job("reconcile", context(ledger, fakes=fakes))["status"], "ok")
        self.assertEqual(ledger.prefix("runs", f"{daemon.FILING_GAP_JOB}:"), [])


class SpendCapTests(IsolatedTest):
    NOW = et("2026-09-28", 9, 30)
    RATES = (2.0, 10.0)  # USD per million input tokens, output tokens

    @staticmethod
    def extraction(created, tokens, provider, output=None, **extra):
        record = {
            "created_at": created,
            "input_tokens": tokens,
            "resolved_model": "jev-1.13.0",
            "spec": {"provider": provider, "requested_model": "jev-1.13.0"},
            **extra,
        }
        if output is not None:
            record["raw"] = {"usage": {"input_tokens": tokens, "output_tokens": output}}
        return record

    def paid(self, cap, *, price=RATES, extractions=(), attempts=()):
        ledger, fakes = FakeLedger(), Fakes()
        for index, row in enumerate(extractions):
            ledger.put("extractions", f"e{index}", {"id": f"e{index}", **self.extraction(*row)})
        for index, (when, provider) in enumerate(attempts):
            ledger.put(
                "attempts",
                f"a{index}",
                {"provider": provider, "requested_model": "jev-1.13.0", "attempted_at": when},
            )
        ctx = context(
            ledger,
            Clock(self.NOW),
            fakes,
            config=conf(provider="jev", spend_cap_usd_month=cap),
            price=lambda provider, model: price,
        )
        return run_job("observe", ctx), fakes

    def worst_request(self):
        return (
            daemon.WORST_CASE_INPUT_TOKENS * self.RATES[0]
            + daemon.MAX_OUTPUT_TOKENS * self.RATES[1]
        ) / 1e6

    def test_zero_cap_or_unknown_price_disables_paid_extraction(self):
        unknown = "No input- and output-token prices are known for jev:jev-1.13.0"
        for cap, price, reason in (
            (0, self.RATES, "spend_cap_usd_month is 0"),
            (5, None, unknown),
            (5, (None, None), unknown),
            (5, (2.0, None), unknown),  # an input price alone cannot bound the answer's cost
            (5, (None, 10.0), unknown),
            (5, 2.0, unknown),
            (5, (0.0, 10.0), unknown),
            (5, (2.0, 0.0), unknown),
            (5, (-1.0, 10.0), unknown),
            (5, (float("nan"), 10.0), unknown),
            (5, (2.0, float("inf")), unknown),
            (5, (True, 10.0), unknown),
            (5, (2.0, 10.0, 1.0), unknown),
        ):
            with self.subTest(cap=cap, price=price):
                record, fakes = self.paid(cap, price=price)
                self.assertEqual(record["status"], "skipped")
                self.assertIn(reason, record["error"])
                self.assertEqual(fakes.calls, [])

    def test_limit_shrinks_to_what_the_remaining_budget_covers(self):
        # Input alone would be $0.06; the 4,096-token answer adds $0.04096 at $10/M.
        self.assertAlmostEqual(self.worst_request(), 0.10096)
        record, fakes = self.paid(5)
        self.assertEqual(fakes.calls, [("observe", "jev", "jev-1.13.0", 10)])
        self.assertEqual(record["counts"]["spend_usd_month"], 0)
        # $0.04 in + $0.01 out spent; $0.25 left covers two worst-case requests, not four.
        record, fakes = self.paid(0.3, extractions=[("2026-09-02T00:00:00Z", 20_000, "jev", 1_000)])
        self.assertEqual(fakes.calls, [("observe", "jev", "jev-1.13.0", 2)])
        self.assertEqual(
            (record["counts"]["spend_usd_month"], record["counts"]["cap_usd_month"]), (0.05, 0.3)
        )

    def test_reaching_the_cap_stops_and_records_why(self):
        # No reported output count: the requested maximum is assumed.
        record, fakes = self.paid(0.2, extractions=[("2026-09-02T00:00:00Z", 100_000, "jev")])
        self.assertEqual(fakes.calls, [])
        self.assertEqual(record["status"], "skipped")
        self.assertIn("Monthly spend cap $0.20 reached (estimated $0.24", record["error"])
        record, fakes = self.paid(0.15, extractions=[("2026-09-02T00:00:00Z", 40_000, "jev", 0)])
        self.assertEqual((record["status"], fakes.calls), ("skipped", []))  # $0.07 left < $0.10

    def test_month_spend_counts_this_utc_month_paid_work_and_failed_attempts(self):
        ledger = FakeLedger()
        rows = [
            ("2026-09-01T00:00:00Z", 10_000, "jev", 500),  # counted
            ("2026-08-31T23:59:59Z", 10_000, "jev", 500),  # last month
            ("2026-09-15T00:00:00Z", 10_000, "rules", 500),  # free
            ("2026-09-15T00:00:00Z", 10_000, "local", 500),  # free
            ("2026-09-15T00:00:00Z", "bogus", "openai"),  # unknown counts: worst case
            # A provider's count below what was sent is floored at two characters per token.
            ("2026-09-16T00:00:00Z", 1_000, "jev", 0, 50_001),
            ("2026-09-17T00:00:00Z", 1_000, "jev", "bogus"),  # unusable output count
        ]
        for index, (created, tokens, provider, *usage) in enumerate(rows):
            output = usage[0] if usage else None
            extra = {"input_chars": usage[1]} if len(usage) > 1 else {}
            record = self.extraction(created, tokens, provider, output, **extra)
            record["resolved_model"] = "m-2026"
            record["spec"]["requested_model"] = "m"
            ledger.put("extractions", f"e{index}", record)
        ledger.put(
            "attempts",
            "a0",
            {"provider": "jev", "requested_model": "m", "attempted_at": "2026-09-20T00:00:00Z"},
        )
        ledger.put(
            "attempts",
            "a1",
            {"provider": "jev", "requested_model": "m", "attempted_at": "2026-08-20T00:00:00Z"},
        )
        # A model with no known price any more (e.g. retired) counts at the service's rates.
        unpriced = self.extraction("2026-09-18T00:00:00Z", 2_000, "openai", 300)
        unpriced.update(
            resolved_model="old-2025", spec={"provider": "openai", "requested_model": "old"}
        )
        ledger.put("extractions", "e-unpriced", unpriced)
        ledger.put(
            "attempts",
            "a2",
            {
                "provider": "openai",
                "requested_model": "old",
                "attempted_at": "2026-09-19T00:00:00Z",
            },
        )
        prices = {("jev", "m"): (1.0, 4.0), ("openai", "m-2026"): (3.0, 12.0)}
        spent = daemon.month_spend(
            ledger,
            now=iso(self.NOW),
            price=lambda provider, model: prices.get((provider, model)),
            default_rates=(100.0, 100.0),
        )
        worst_in, worst_out = daemon.WORST_CASE_INPUT_TOKENS, daemon.MAX_OUTPUT_TOKENS
        expected = (
            (10_000 * 1.0 + 500 * 4.0)
            + (worst_in * 3.0 + worst_out * 12.0)
            + 25_001 * 1.0
            + (1_000 * 1.0 + worst_out * 4.0)
            + (worst_in * 1.0 + worst_out * 4.0)
            + (2_000 * 100.0 + 300 * 100.0)
            + (worst_in * 100.0 + worst_out * 100.0)
        ) / 1e6
        self.assertAlmostEqual(spent, expected)

    def test_registry_price_reads_the_declared_fields_or_none(self):
        class Registry:
            @staticmethod
            def lookup(provider, model):
                if model == "missing":
                    raise ValueError("unknown")
                if provider != "jev":
                    return {}
                return {"usd_per_million_input_tokens": 1.5, "usd_per_million_output_tokens": 6.0}

        with patch.object(daemon.registry, "lookup", Registry.lookup):
            self.assertEqual(daemon.registry_price("jev", "jev-1.13.0"), (1.5, 6.0))
            self.assertEqual(daemon.registry_price("openai", "gpt"), (None, None))
            self.assertIsNone(daemon.registry_price("jev", "missing"))
        with patch.object(daemon.registry, "lookup", side_effect=KeyError("jev")):
            self.assertIsNone(daemon.registry_price("jev", "jev-1.13.0"))
        self.assertEqual(daemon.registry_price("jev", "jev-1.13.0"), (None, None))
        self.assertEqual(daemon.registry_price("rules", "rules-v1"), (0.0, 0.0))


class StateTests(IsolatedTest):
    def test_load_state_restores_attempts_and_completions(self):
        ledger, fakes = FakeLedger(), Fakes()
        clock = Clock(et("2026-09-28", 8, 45))
        run_job("brief", context(ledger, clock, fakes))
        clock.advance(60)
        fakes.poll = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        failed = run_job("poll", context(ledger, clock, fakes))
        restored = daemon.load_state(ledger)
        self.assertEqual(
            restored,
            {
                "attempted": {"brief": iso(et("2026-09-28", 8, 45)), "poll": failed["started_at"]},
                "completed": {"brief": iso(et("2026-09-28", 8, 45))},
                "pending": {"observe": True},
                "backoff": {"poll": True},
            },
        )
        # A restart the same morning must not send the brief again.
        self.assertNotIn("brief", due_jobs(et("2026-09-28", 9, 0), restored, conf()))
        self.assertEqual(set(daemon.last_runs(ledger)), {"brief", "poll"})

    def test_remember_sets_pending_observe_from_new_work(self):
        current = {}
        daemon.remember(
            current, {"job": "poll", "status": "ok", "started_at": "t1", "counts": {"added": 0}}
        )
        self.assertFalse(current["pending"].get("observe"))
        daemon.remember(
            current,
            {"job": "bars", "status": "partial", "started_at": "t2", "counts": {"added": 3}},
        )
        self.assertTrue(current["pending"]["observe"])
        full = {
            "job": "observe",
            "status": "ok",
            "started_at": "t3",
            "counts": {"forecasts": 10, "limit": 10},
        }
        daemon.remember(current, full)
        self.assertTrue(current["pending"]["observe"])
        for status, forecasts in (("ok", 3), ("partial", 10), ("failed", 0), ("skipped", 0)):
            with self.subTest(status=status):
                daemon.remember(
                    current,
                    {**full, "status": status, "counts": {"forecasts": forecasts, "limit": 10}},
                )
                self.assertFalse(current["pending"]["observe"])
        self.assertEqual(current["completed"]["observe"], "t3")  # failed is not completion
        self.assertEqual(current["attempted"]["observe"], "t3")


class LoopTests(IsolatedTest):
    def run_loop(self, ctx, clock, *, ticks, tick=10, jumps=None, stop=None):
        stop = stop or threading.Event()
        counter = {"n": 0}

        def sleep(seconds):
            counter["n"] += 1
            clock.advance(seconds + (jumps or {}).get(counter["n"], 0))
            if counter["n"] >= ticks:
                stop.set()

        daemon.run_forever(
            ctx, sleep=sleep, stop=stop, lock_path=self.dir / "daemon.lock", tick_seconds=tick
        )
        return counter["n"]

    def test_loop_runs_jobs_in_order_and_records_them(self):
        clock = Clock(et("2026-09-28", 16, 29, 50))
        ledger, fakes = FakeLedger(), Fakes()
        ctx = context(ledger, clock, fakes)
        ctx.brief = lambda *a, **k: {"filings": []}  # the brief slot is due too
        self.run_loop(ctx, clock, ticks=8)
        # 16:29:50, first start: poll, Friday's bars catch-up, observe, settle, the brief
        # (08:45 has passed) and Friday's reconcile. 16:30:00: Monday's bars, then settle.
        # 16:30:50: the next poll and, since bars added data, observe.
        self.assertEqual(
            fakes.jobs(),
            ["poll", "bars", "observe", "settle", "render", "notify", "reconcile"]
            + ["bars", "settle", "poll", "observe"],
        )
        self.assertEqual(
            [(r["job"], r["started_at"][11:19]) for r in ledger.all("runs")],
            [
                ("bars", "20:29:50"),
                ("bars", "20:30:00"),
                ("brief", "20:29:50"),
                ("observe", "20:29:50"),
                ("observe", "20:30:50"),
                ("poll", "20:29:50"),
                ("poll", "20:30:50"),
                ("reconcile", "20:29:50"),
                ("settle", "20:29:50"),
                ("settle", "20:30:00"),
            ],
        )

    def test_second_daemon_is_refused_at_once_and_lock_is_released_after_stop(self):
        lock = self.dir / "daemon.lock"
        ledger, sleeps = FakeLedger(), []
        with daemon.single_writer(lock, role="daemon"):
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
            self.assertEqual(lock.read_text(), f"{os.getpid()} daemon\n")
            with self.assertRaisesRegex(daemon.AlreadyRunning, "already running") as refused:
                daemon.run_forever(context(ledger), lock_path=lock, sleep=sleeps.append)
        self.assertEqual((refused.exception.holder, sleeps), ("daemon", []))
        self.assertEqual(ledger.records, {})
        stop = threading.Event()
        stop.set()
        daemon.run_forever(context(ledger), lock_path=lock, stop=stop, sleep=lambda s: None)
        handle = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free again
        finally:
            os.close(handle)
        with self.assertRaises(ValueError):
            with daemon.single_writer(lock, role="other"):
                pass

    def test_daemon_waits_for_a_one_off_job_instead_of_exiting(self):
        # launchd restarts only failed exits: a clean exit here would leave the service down.
        lock, stop = self.dir / "daemon.lock", threading.Event()
        ledger, clock = FakeLedger(), Clock(et("2026-09-28", 10, 0))
        job = contextlib.ExitStack()
        job.enter_context(daemon.single_writer(lock, role="job"))  # a terminal `poll`
        self.addCleanup(job.close)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                job.close()  # the job finishes
            elif len(sleeps) > 3:
                stop.set()

        ctx = context(ledger, clock, config=conf(bars_source="none"))
        ctx.brief = lambda *a, **k: {"filings": []}
        daemon.run_forever(ctx, lock_path=lock, sleep=sleep, stop=stop)
        self.assertEqual(sleeps[:3], [daemon.LOCK_RETRY_SECONDS] * 3)
        self.assertEqual(len(ledger.prefix("runs", "poll:")), 1)
        self.assertEqual(daemon.last_run(ledger, "poll")["started_at"], clock())

    def test_a_lock_held_past_the_wait_is_refused_so_launchd_retries(self):
        lock = self.dir / "daemon.lock"
        for content, holder in (("123 job\n", "job"), ("123\n", None), ("", None)):
            with self.subTest(content=content):
                # Another process's lock; an older version wrote only its pid.
                handle = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.write(handle, content.encode())
                    sleeps = []
                    with self.assertRaises(daemon.AlreadyRunning) as refused:
                        daemon.run_forever(context(), lock_path=lock, sleep=sleeps.append)
                finally:
                    os.close(handle)
                self.assertEqual(refused.exception.holder, holder)
                self.assertEqual(sum(sleeps), daemon.LOCK_WAIT_SECONDS)  # waited, then gave up
        stop, sleeps = threading.Event(), []

        def interrupted(seconds):
            sleeps.append(seconds)
            stop.set()  # SIGTERM while waiting

        with daemon.single_writer(lock, role="job"):
            ledger = FakeLedger()
            self.assertIsNone(
                daemon.run_forever(context(ledger), lock_path=lock, sleep=interrupted, stop=stop)
            )
        self.assertEqual((sleeps, ledger.records), ([daemon.LOCK_RETRY_SECONDS], {}))

    def test_lock_defaults_to_the_app_directory(self):
        stop = threading.Event()
        stop.set()
        daemon.run_forever(context(), stop=stop, sleep=lambda s: None)
        self.assertTrue((self.dir / "home" / "daemon.lock").exists())

    def test_sleep_gap_is_recorded_and_polling_catches_up(self):
        clock = Clock(et("2026-09-28", 10, 0))
        ledger, fakes = FakeLedger(), Fakes()
        ctx = context(ledger, clock, fakes, config=conf(bars_source="none"))
        ctx.brief = lambda *a, **k: {"filings": []}
        # The second sleep lasts 40 extra minutes (the lid was closed).
        self.run_loop(ctx, clock, ticks=3, jumps={2: 2400})
        gaps = ledger.prefix("runs", "coverage_gap:")
        self.assertEqual(len(gaps), 1)
        gap = gaps[0]
        self.assertEqual((gap["status"], gap["counts"]), ("gap", {"seconds": 2410}))
        self.assertEqual(gap["started_at"], iso(et("2026-09-28", 10, 0, 10)))
        self.assertEqual(gap["finished_at"], iso(et("2026-09-28", 10, 40, 20)))
        self.assertIn("slept", gap["error"])
        polls = [r["started_at"] for r in ledger.prefix("runs", "poll:")]
        self.assertEqual(polls, [iso(et("2026-09-28", 10, 0)), iso(et("2026-09-28", 10, 40, 20))])

    def loop_with_slow_observe(self, start, seconds, *, ticks, jumps=None):
        clock = Clock(start)
        ledger, fakes = FakeLedger(), Fakes()
        ctx = context(ledger, clock, fakes, config=conf(bars_source="none"))
        ctx.brief = lambda *a, **k: {"filings": []}

        def slow_observe(*args, **kwargs):
            clock.advance(seconds)  # one slow event, or the Mac asleep mid-request
            return {"forecasts": [], "errors": [], "skipped": {}}

        ctx.observe = slow_observe
        self.run_loop(ctx, clock, ticks=ticks, jumps=jumps)
        gaps = [
            (r["started_at"], r["finished_at"], r["counts"]["seconds"], r["error"])
            for r in ledger.prefix("runs", "coverage_gap:")
        ]
        return gaps, [r["started_at"] for r in ledger.prefix("runs", "poll:")]

    def test_no_gap_for_ordinary_ticks_or_short_jobs(self):
        gaps, polls = self.loop_with_slow_observe(et("2026-09-28", 10, 0), 200, ticks=5)
        self.assertEqual(gaps, [])
        self.assertEqual(len(polls), 5)

    def test_jobs_that_stall_polling_are_a_coverage_gap(self):
        # Tuesday afternoon, the busiest filing hours: each batch holds polling for 45 minutes.
        gaps, polls = self.loop_with_slow_observe(et("2026-09-29", 16, 0), 2700, ticks=2)
        reason = "Not watching: jobs ran long or the computer slept"
        self.assertEqual(
            gaps,
            [
                (iso(et("2026-09-29", 16, 0)), iso(et("2026-09-29", 16, 45)), 2700, reason),
                (
                    iso(et("2026-09-29", 16, 45, 10)),
                    iso(et("2026-09-29", 17, 30, 10)),
                    2700,
                    reason,
                ),
            ],
        )
        # Polling catches up at the next tick after each gap.
        self.assertEqual(polls, [iso(et("2026-09-29", 16, 0)), iso(et("2026-09-29", 16, 45, 10))])

    def test_a_slow_job_and_a_short_sleep_add_up_to_a_gap(self):
        # 200 s of work, then a sleep 200 s late: neither alone, but polling paused 410 s.
        gaps, polls = self.loop_with_slow_observe(
            et("2026-09-28", 10, 0), 200, ticks=2, jumps={1: 200}
        )
        self.assertEqual(
            [gap[:3] for gap in gaps],
            [(iso(et("2026-09-28", 10, 0)), iso(et("2026-09-28", 10, 6, 50)), 410)],
        )
        self.assertEqual(polls[1], iso(et("2026-09-28", 10, 6, 50)))

    def test_polling_backoff_is_not_a_stall(self):
        clock = Clock(et("2026-09-28", 10, 0))
        ledger, fakes = FakeLedger(), Fakes()
        fakes.poll_result = {**fakes.poll_result, "added": [], "stopped": True}  # SEC said wait
        ctx = context(ledger, clock, fakes, config=conf(bars_source="none"))
        ctx.brief = lambda *a, **k: {"filings": []}
        self.run_loop(ctx, clock, ticks=100, tick=10)  # about 17 minutes
        self.assertEqual(ledger.prefix("runs", "coverage_gap:"), [])
        self.assertEqual(len(ledger.prefix("runs", "poll:")), 2)  # 10:00, then 10:15

    def test_startup_after_downtime_records_a_gap_but_not_after_a_quiet_interval(self):
        ledger, fakes = FakeLedger(), Fakes()
        old = Clock(et("2026-09-26", 12, 0))  # Saturday: 15-minute polls
        run_job("poll", context(ledger, old, fakes))
        stop = threading.Event()
        stop.set()
        quiet = Clock(et("2026-09-26", 12, 19, 59))  # within 15 + 5 minutes
        daemon.run_forever(context(ledger, quiet), clock=quiet, stop=stop, lock_path=self.dir / "l")
        self.assertEqual(ledger.prefix("runs", "coverage_gap:"), [])
        later = Clock(et("2026-09-26", 14, 0))
        daemon.run_forever(context(ledger, later), stop=stop, lock_path=self.dir / "l")
        gap = ledger.prefix("runs", "coverage_gap:")[0]
        self.assertEqual(gap["counts"], {"seconds": 7200})
        self.assertIn("not running", gap["error"])

    def test_restart_does_not_repeat_the_morning_brief(self):
        ledger, fakes = FakeLedger(), Fakes()
        clock = Clock(et("2026-09-28", 8, 45))
        run_job("brief", context(ledger, clock, fakes))
        clock.advance(600)
        fakes2 = Fakes()
        self.run_loop(
            context(ledger, clock, fakes2, config=conf(bars_source="none")), clock, ticks=1
        )
        self.assertNotIn("brief", fakes2.jobs())
        self.assertIn("poll", fakes2.jobs())

    def test_stop_between_jobs_ends_the_tick(self):
        clock = Clock(et("2026-09-28", 17, 0))
        stop = threading.Event()
        fakes = Fakes()
        ctx = context(clock=clock, fakes=fakes)

        def poll(*args, **kwargs):
            stop.set()  # e.g. SIGTERM while polling
            return fakes.poll_result

        ctx.poll = poll
        daemon.run_forever(ctx, stop=stop, sleep=lambda s: None, lock_path=self.dir / "l")
        self.assertEqual(fakes.jobs(), [])  # nothing after the interrupted poll


if __name__ == "__main__":
    unittest.main()
