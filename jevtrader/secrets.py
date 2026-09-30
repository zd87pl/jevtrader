"""API keys from the environment or an OS secret store; values never reach argv, logs or errors.

Stores: the macOS Keychain, libsecret (``secret-tool``), the Windows Credential Manager (through
PowerShell) and read-only Docker secrets. Every backend runs its tool through an injected runner.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from . import paths
from .security import childenv

SERVICE = paths.APP_NAME
KNOWN = ("TYPESAFE_API_KEY", "OPENAI_API_KEY", "ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
SECURITY = "/usr/bin/security"
NOT_FOUND = 44  # errSecItemNotFound as a `security` exit status.
SECRET_TOOL = "secret-tool"
POWERSHELL = "powershell"
DOCKER_SECRETS_DIR = Path("/run/secrets")
SECRETS_DIR_ENV = "JEVTRADER_SECRETS_DIR"
TIMEOUT_SECONDS = 30
# Child environments add only what these tools need to reach the user's session (P0-42).
CHILD_ENV_EXTRA = (
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "SYSTEMROOT",
    "WINDIR",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
)
# `security -i` tokenizes its input line; this set needs no quoting and cannot start an option.
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~+/=:-]{0,511}")

Runner = Callable[..., Any]  # runner(argv: list[str], input: str | None) -> .returncode, .stdout


def run(argv: list[str], input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        input=input,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
        env=childenv.scrubbed(extra=CHILD_ENV_EXTRA),
    )


def keychain_available() -> bool:
    return sys.platform == "darwin" and os.path.exists(SECURITY)


def _name(name: str) -> str:
    if name not in KNOWN:
        raise ValueError(f"Unknown secret name; expected one of: {', '.join(KNOWN)}")
    return name


def _check_value(name: str, value: object) -> str:
    if not isinstance(value, str) or not _VALUE.fullmatch(value):
        raise ValueError(
            f"{name} must be 1–512 characters of letters, digits and ._~+/=:- "
            "starting with a letter or digit"
        )
    return value


def _call(runner: Runner, argv: list[str], stdin: str | None = None, *, what: str) -> Any:
    try:
        return runner(argv, stdin)
    except (OSError, subprocess.SubprocessError):
        # Exception text can echo the command's input; report only what failed.
        raise RuntimeError(f"{what} could not run") from None


class SecretStore(Protocol):
    """One OS secret store; values travel on stdin or in files, never in argv."""

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> bool: ...


class KeychainStore:
    """The macOS login Keychain through /usr/bin/security."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner

    def _run(self) -> Runner:
        return self._runner or run

    def get(self, name: str) -> str | None:
        name = _name(name)
        argv = [SECURITY, "find-generic-password", "-s", SERVICE, "-a", name, "-w"]
        result = _call(self._run(), argv, what=f"Keychain command {argv[1]}")
        if result.returncode == NOT_FOUND:
            return None
        if result.returncode != 0:
            raise RuntimeError(f"Keychain lookup for {name} failed (exit {result.returncode})")
        return (result.stdout or "").rstrip("\r\n") or None

    def set(self, name: str, value: str) -> None:
        name = _name(name)
        value = _check_value(name, value)
        # Values go through stdin: argv is visible to every local process via `ps`.
        command = f"add-generic-password -U -s {SERVICE} -a {name} -w {value}\n"
        result = _call(self._run(), [SECURITY, "-i"], command, what="Keychain command -i")
        # Interactive mode can exit 0 after a failed command; confirm by reading it back.
        if result.returncode != 0 or self.get(name) != value:
            raise RuntimeError(f"Keychain did not store {name}")

    def delete(self, name: str) -> bool:
        name = _name(name)
        argv = [SECURITY, "delete-generic-password", "-s", SERVICE, "-a", name]
        result = _call(self._run(), argv, what=f"Keychain command {argv[1]}")
        if result.returncode == NOT_FOUND:
            return False
        if result.returncode != 0:
            raise RuntimeError(f"Keychain delete for {name} failed (exit {result.returncode})")
        return True


class LibsecretStore:
    """The freedesktop Secret Service (GNOME Keyring, KWallet) through ``secret-tool``."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner

    def _run(self) -> Runner:
        return self._runner or run

    def _attributes(self, name: str) -> list[str]:
        return ["service", SERVICE, "account", name]

    def get(self, name: str) -> str | None:
        name = _name(name)
        argv = [SECRET_TOOL, "lookup", *self._attributes(name)]
        result = _call(self._run(), argv, what="secret-tool lookup")
        if result.returncode != 0:
            # secret-tool exits 1 silently when nothing matches; anything else is a failure.
            if not (getattr(result, "stderr", "") or getattr(result, "stdout", "")):
                return None
            raise RuntimeError(f"libsecret lookup for {name} failed (exit {result.returncode})")
        return (result.stdout or "").rstrip("\r\n") or None

    def set(self, name: str, value: str) -> None:
        name = _name(name)
        label = f"{SERVICE} {name}"
        argv = [SECRET_TOOL, "store", "--label", label, *self._attributes(name)]
        result = _call(self._run(), argv, value, what="secret-tool store")
        if result.returncode != 0 or self.get(name) != value:
            raise RuntimeError(f"libsecret did not store {name}")

    def delete(self, name: str) -> bool:
        name = _name(name)
        existed = self.get(name) is not None
        argv = [SECRET_TOOL, "clear", *self._attributes(name)]
        result = _call(self._run(), argv, what="secret-tool clear")
        if result.returncode != 0:
            raise RuntimeError(f"libsecret delete for {name} failed (exit {result.returncode})")
        return existed


# A fixed script: the operation, name and value arrive on stdin, so argv never varies.
WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class JevtraderCred {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  public struct CREDENTIAL {
    public int Flags; public int Type; public string TargetName; public string Comment;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
    public int CredentialBlobSize; public IntPtr CredentialBlob; public int Persist;
    public int AttributeCount; public IntPtr Attributes; public string TargetAlias;
    public string UserName;
  }
  [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  public static extern bool CredRead(string target, int type, int flags, out IntPtr cred);
  [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  public static extern bool CredWrite(ref CREDENTIAL cred, int flags);
  [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  public static extern bool CredDelete(string target, int type, int flags);
  [DllImport("advapi32.dll")]
  public static extern void CredFree(IntPtr cred);
  public static string Read(string target) {
    IntPtr p;
    if (!CredRead(target, 1, 0, out p)) {
      if (Marshal.GetLastWin32Error() == 1168) { return null; }
      throw new InvalidOperationException("CredRead failed");
    }
    try {
      CREDENTIAL c = (CREDENTIAL)Marshal.PtrToStructure(p, typeof(CREDENTIAL));
      return Marshal.PtrToStringUni(c.CredentialBlob, c.CredentialBlobSize / 2);
    } finally { CredFree(p); }
  }
  public static bool Write(string target, string user, string secret) {
    CREDENTIAL c = new CREDENTIAL();
    c.Type = 1; c.TargetName = target; c.UserName = user; c.Persist = 2;
    c.CredentialBlobSize = secret.Length * 2;
    c.CredentialBlob = Marshal.StringToCoTaskMemUni(secret);
    try { return CredWrite(ref c, 0); }
    finally { Marshal.ZeroFreeCoTaskMemUnicode(c.CredentialBlob); }
  }
  public static int Delete(string target) {
    if (CredDelete(target, 1, 0)) { return 0; }
    return Marshal.GetLastWin32Error() == 1168 ? 44 : 1;
  }
}
'@
$op = [Console]::In.ReadLine()
$name = [Console]::In.ReadLine()
$target = 'jevtrader:' + $name
if ($op -eq 'get') {
  $v = [JevtraderCred]::Read($target)
  if ($null -eq $v) { exit 44 }
  [Console]::Out.Write($v); exit 0
}
if ($op -eq 'set') {
  $v = [Console]::In.ReadLine()
  if ([JevtraderCred]::Write($target, $name, $v)) { exit 0 }
  exit 1
}
if ($op -eq 'delete') { exit [JevtraderCred]::Delete($target) }
exit 2
"""


class WindowsStore:
    """The Windows Credential Manager through PowerShell and a fixed P/Invoke script."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner

    def _call(self, request: str, op: str) -> Any:
        encoded = base64.b64encode(WINDOWS_SCRIPT.encode("utf-16-le")).decode("ascii")
        argv = [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        return _call(self._runner or run, argv, request, what=f"Credential Manager {op}")

    def get(self, name: str) -> str | None:
        name = _name(name)
        result = self._call(f"get\n{name}\n", "get")
        if result.returncode == NOT_FOUND:
            return None
        if result.returncode != 0:
            raise RuntimeError(
                f"Credential Manager lookup for {name} failed (exit {result.returncode})"
            )
        return (result.stdout or "").rstrip("\r\n") or None

    def set(self, name: str, value: str) -> None:
        name = _name(name)
        value = _check_value(name, value)
        result = self._call(f"set\n{name}\n{value}\n", "set")
        if result.returncode != 0 or self.get(name) != value:
            raise RuntimeError(f"Credential Manager did not store {name}")

    def delete(self, name: str) -> bool:
        name = _name(name)
        result = self._call(f"delete\n{name}\n", "delete")
        if result.returncode == NOT_FOUND:
            return False
        if result.returncode != 0:
            raise RuntimeError(
                f"Credential Manager delete for {name} failed (exit {result.returncode})"
            )
        return True


class DockerSecretsStore:
    """Read-only files mounted by Docker or Compose: ``<dir>/<name lowercased>``."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory is not None else DOCKER_SECRETS_DIR

    def get(self, name: str) -> str | None:
        name = _name(name)
        path = self.directory / name.lower()
        try:
            if not path.is_file():
                return None
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise RuntimeError(f"Docker secret {name.lower()} could not be read") from None
        return text.rstrip("\r\n") or None

    def set(self, name: str, value: str) -> None:
        raise ValueError(f"Docker secrets are read-only; mount {_name(name).lower()} instead")

    def delete(self, name: str) -> bool:
        raise ValueError(f"Docker secrets are read-only; unmount {_name(name).lower()} instead")


def choose_store() -> SecretStore | None:
    """Docker secrets when mounted or configured, else the platform store, else None."""
    override = os.environ.get(SECRETS_DIR_ENV, "").strip()
    if override:
        return DockerSecretsStore(Path(override))
    if DOCKER_SECRETS_DIR.is_dir():
        return DockerSecretsStore(DOCKER_SECRETS_DIR)
    if sys.platform == "darwin":
        return KeychainStore() if keychain_available() else None
    if sys.platform.startswith("linux"):
        # secret-tool needs a session bus; headless hosts use the environment or Docker.
        if shutil.which(SECRET_TOOL) and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return LibsecretStore()
        return None
    if sys.platform == "win32" and shutil.which(POWERSHELL):
        return WindowsStore()
    return None


def _resolve(runner: Runner | None, store: SecretStore | None) -> SecretStore | None:
    if store is not None:
        return store
    if runner is not None:
        return KeychainStore(runner)  # the historical seam: a runner means the Keychain
    return choose_store()


def _require(name: str, runner: Runner | None, store: SecretStore | None) -> SecretStore:
    chosen = _resolve(runner, store)
    if chosen is None:
        raise ValueError(
            "The macOS Keychain is unavailable here and no other secret store was found; "
            f"export {name} instead"
        )
    return chosen


def get(name: str, *, runner: Runner | None = None, store: SecretStore | None = None) -> str | None:
    """The environment wins so a shell or service override needs no stored entry."""
    name = _name(name)
    if os.environ.get(name):
        return os.environ[name]
    chosen = _resolve(runner, store)
    return chosen.get(name) if chosen is not None else None


def set(
    name: str, value: str, *, runner: Runner | None = None, store: SecretStore | None = None
) -> None:
    name = _name(name)
    value = _check_value(name, value)
    _require(name, runner, store).set(name, value)


def delete(name: str, *, runner: Runner | None = None, store: SecretStore | None = None) -> bool:
    name = _name(name)
    return _require(name, runner, store).delete(name)


def export_to_environ(
    names: Iterable[str] = KNOWN,
    *,
    runner: Runner | None = None,
    store: SecretStore | None = None,
) -> list[str]:
    """Fill unset variables from the secret store; returns the names loaded, never values."""
    names = [_name(name) for name in names]
    if not names:
        return []
    chosen = _resolve(runner, store)
    if chosen is None:
        return []
    loaded = []
    for name in names:
        if os.environ.get(name):
            continue
        value = chosen.get(name)
        if value:
            os.environ[name] = value
            loaded.append(name)
    return loaded


def _require_keychain(name: str) -> Runner:
    if not keychain_available():
        raise ValueError(f"The macOS Keychain is unavailable here; export {name} instead")
    return run


# Private aliases kept until every caller patches the public seams (P0-26, #30).
# Patching an alias does not change what the module calls.
_run = run
_keychain_available = keychain_available
