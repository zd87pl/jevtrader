"""A request pace shared by every process that uses the same app directory.

The daemon, ``backfill`` and ``collect`` can run at once, so a per-process limiter
lets their combined SEC or Alpaca rate add up. Each named limiter keeps the next free
request slot in a small JSON file under ``<app dir>/ratelimit/``. A caller takes an
exclusive ``fcntl.flock`` on that file, reserves the next slot, releases the lock and
then sleeps until its slot, so waiting never holds the lock. Slots are wall-clock
seconds because monotonic clocks are not comparable between processes; a stored slot
far in the future (a clock step backwards, or a corrupt file) is discarded.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from . import paths

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_MAX_AHEAD = 60.0  # A reservation further ahead than this is treated as stale.


def state_path(name: str) -> Path:
    return paths.app_dir() / "ratelimit" / f"{name}.json"


def _stored_slot(handle: int) -> float | None:
    os.lseek(handle, 0, os.SEEK_SET)
    raw = os.read(handle, 4096)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    slot = value.get("next") if isinstance(value, dict) else None
    if isinstance(slot, bool) or not isinstance(slot, (int, float)) or not math.isfinite(slot):
        return None
    return float(slot)


class SharedLimiter:
    """At most one request per ``interval`` seconds across threads and processes."""

    def __init__(
        self,
        name: str,
        interval: float,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        if not _NAME.fullmatch(name):
            raise ValueError("Limiter name must be a short lowercase identifier")
        if not isinstance(interval, (int, float)) or not 0 < interval <= _MAX_AHEAD:
            raise ValueError("Limiter interval must be a positive number of seconds")
        self.name = name
        self.interval = float(interval)
        self.clock = clock
        self.sleep = sleep

    def _reserve(self) -> tuple[float, float]:
        path = state_path(self.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            now = self.clock()
            stored = _stored_slot(handle)
            if stored is None or stored > now + _MAX_AHEAD:
                stored = now
            slot = max(now, stored)
            data = json.dumps({"next": slot + self.interval}).encode()
            os.ftruncate(handle, 0)
            os.lseek(handle, 0, os.SEEK_SET)
            os.write(handle, data)
            return now, slot
        finally:
            os.close(handle)  # Closing the descriptor releases the lock.

    def acquire(self) -> None:
        now, slot = self._reserve()
        if slot > now:
            self.sleep(slot - now)
