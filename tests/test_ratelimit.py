"""The SEC and Alpaca limiters are shared by every process that uses the same app directory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

from jevtrader import bars, paths, ratelimit, sec

ROOT = Path(__file__).resolve().parents[1]

CHILD = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from urllib.request import Request

    from jevtrader import bars, sec

    kind, count, go, out = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
    stamps = []

    class Response:
        def __init__(self, url):
            self.url = url
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def geturl(self):
            return self.url
        def read(self, size):
            return b"{}"

    def transport(request, timeout):
        stamps.append(time.time())
        return Response(request.full_url)

    # Imports are done; tell the parent, then wait for the shared start.
    Path(str(out) + ".ready").write_text("")
    deadline = time.time() + 20
    while not go.exists() and time.time() < deadline:
        time.sleep(0.005)
    if kind == "sec":
        client = sec._SECClient("Offline test a@b.test", 5, count, transport=transport)
        for _ in range(count):
            client.get("https://data.sec.gov/submissions/CIK0000123456.json")
    else:
        for _ in range(count):
            bars._LIMITER.acquire()
            stamps.append(time.time())
    out.write_text(json.dumps(stamps))
    """
)


def most_in_window(stamps: list[float], window: float) -> int:
    stamps = sorted(stamps)
    best = 0
    start = 0
    for end, value in enumerate(stamps):
        while value - stamps[start] >= window:
            start += 1
        best = max(best, end - start + 1)
    return best


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class SharedLimiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(os.environ[paths.HOME_ENV])
        self.clock = Clock()

    def limiter(self, name: str = "sec", interval: float = 0.25) -> ratelimit.SharedLimiter:
        return ratelimit.SharedLimiter(
            name, interval, clock=self.clock.time, sleep=self.clock.sleep
        )

    def test_two_limiters_on_one_state_file_share_the_schedule(self) -> None:
        first, second = self.limiter(), self.limiter()
        first.acquire()
        second.acquire()
        first.acquire()
        self.assertEqual(self.clock.slept, [0.25, 0.25])
        state = ratelimit.state_path("sec")
        self.assertTrue(state.is_relative_to(self.home))
        self.assertAlmostEqual(json.loads(state.read_text())["next"], 1_000.75)

    def test_limiters_with_different_names_do_not_share(self) -> None:
        self.limiter("sec").acquire()
        self.limiter("alpaca").acquire()
        self.assertEqual(self.clock.slept, [])

    def test_idle_time_is_not_banked(self) -> None:
        limiter = self.limiter()
        limiter.acquire()
        self.clock.now += 10
        limiter.acquire()
        limiter.acquire()
        self.assertEqual(self.clock.slept, [0.25])

    def test_corrupt_or_far_future_state_is_reset(self) -> None:
        state = ratelimit.state_path("sec")
        state.parent.mkdir(parents=True, exist_ok=True)
        for text in ["not json", "[]", '{"next": "x"}', '{"next": NaN}', '{"next": 1e12}']:
            with self.subTest(text=text):
                state.write_text(text)
                self.clock.slept.clear()
                self.limiter().acquire()
                self.assertEqual(self.clock.slept, [])

    def test_invalid_name_or_interval_is_refused(self) -> None:
        for name, interval in [("../x", 0.2), ("", 0.2), ("sec", 0), ("sec", float("nan"))]:
            with self.subTest(name=name, interval=interval), self.assertRaises(ValueError):
                ratelimit.SharedLimiter(name, interval)

    def test_default_limiters_are_shared_and_within_documented_limits(self) -> None:
        self.assertIsInstance(sec._DEFAULT_LIMITER, ratelimit.SharedLimiter)
        self.assertIsInstance(bars._LIMITER, ratelimit.SharedLimiter)
        self.assertGreaterEqual(sec._DEFAULT_LIMITER.interval, 0.1)
        # Alpaca's free market-data tier allows 200 requests per minute.
        self.assertGreaterEqual(bars._LIMITER.interval * 200, 60.0)


class CrossProcessTests(unittest.TestCase):
    """Two real interpreters, a fake transport, and one shared schedule."""

    def run_children(self, kind: str, count: int) -> list[list[float]]:
        home = Path(os.environ[paths.HOME_ENV])
        home.mkdir(parents=True, exist_ok=True)
        go = home / f"{kind}.go"
        outs = [home / f"{kind}-{index}.json" for index in range(2)]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT), *filter(None, [env.get("PYTHONPATH", "")])])
        children = [
            subprocess.Popen(
                [sys.executable, "-c", CHILD, kind, str(count), str(go), str(out)],
                cwd=ROOT,
                env=env,
            )
            for out in outs
        ]
        # Start both schedules only after both interpreters have imported, so
        # a slow start under load cannot keep the two runs from overlapping.
        ready = [Path(str(out) + ".ready") for out in outs]
        deadline = time.monotonic() + 20
        while not all(flag.exists() for flag in ready):
            if time.monotonic() > deadline or any(c.poll() is not None for c in children):
                break
            time.sleep(0.005)
        go.write_text("")
        for child in children:
            self.assertEqual(child.wait(timeout=30), 0)
        return [json.loads(out.read_text()) for out in outs]

    def assert_coordinated(self, runs: list[list[float]], interval: float) -> None:
        stamps = sorted(runs[0] + runs[1])
        first, second = runs
        # The processes overlapped, so only a shared schedule explains the spacing.
        self.assertLess(max(min(first), min(second)), min(max(first), max(second)))
        # Slots are exactly one interval apart, but a busy runner can wake a request late, which
        # shortens the measured span. Allow one interval of that jitter: without a shared
        # schedule the two runs would interleave freely and span only about half of this.
        self.assertGreaterEqual(stamps[-1] - stamps[0], (len(stamps) - 2) * interval)

    def test_sec_stays_at_or_below_ten_per_second_across_processes(self) -> None:
        runs = self.run_children("sec", 6)
        self.assert_coordinated(runs, sec.REQUEST_INTERVAL)
        self.assertLessEqual(most_in_window(runs[0] + runs[1], 1.0), 10)

    def test_alpaca_stays_within_its_tier_across_processes(self) -> None:
        runs = self.run_children("alpaca", 4)
        self.assert_coordinated(runs, bars.REQUEST_INTERVAL)
        # 200 per minute is 3.3 per second; one late wake-up may add a fifth.
        self.assertLessEqual(most_in_window(runs[0] + runs[1], 1.0), 5)


if __name__ == "__main__":
    unittest.main()
