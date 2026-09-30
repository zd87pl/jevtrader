"""Scrubbed environments for every child process the package starts (P0-42, ADR-0008).

A child gets only an allowlist: ``PATH``, ``HOME``, ``LANG``, ``LC_*``, ``TMPDIR``, ``USER``,
``LOGNAME`` and ``SHELL``, plus any per-runner ``extra`` names (a session bus address, say).
A name that looks like a credential never passes, even when a caller lists it in ``extra``,
so API keys exported for this process never reach osascript, launchctl, systemctl,
``security``, ``secret-tool`` or PowerShell.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

ALLOWED = frozenset({"PATH", "HOME", "LANG", "TMPDIR", "USER", "LOGNAME", "SHELL"})
_LOCALE = re.compile(r"LC_[A-Z]+")
_SECRETISH = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH|PASS", re.IGNORECASE)


def scrubbed(
    environ: Mapping[str, str] | None = None, *, extra: Iterable[str] = ()
) -> dict[str, str]:
    """The allowlisted subset of ``environ`` (default: ``os.environ``); never a key."""
    source = os.environ if environ is None else environ
    allowed = ALLOWED | frozenset(extra)
    return {
        name: value
        for name, value in source.items()
        if (name in allowed or _LOCALE.fullmatch(name)) and not _SECRETISH.search(name)
    }
