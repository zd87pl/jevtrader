# Secrets policy

Status: binding for all code and docs. It implements ADR-0001 D4 and audit invariant I-25, and it
is the target for #28 (cross-platform store) and #46 (broker-key isolation). Citations are checked
by `tests/test_security_docs.py`. Threats and mitigations are in [threat-model.md](threat-model.md).

## Inventory

The only secrets are the four names in `KNOWN` (`jevtrader/secrets.py:23`):

| Name | Purpose | Needed by |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | JEV text provider | extraction commands |
| `OPENAI_API_KEY` | OpenAI text provider | extraction commands |
| `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | Alpaca bars; paper-account keys, so **trading-capable** | bars, backfill `--bars`, daemon |

The Alpaca keys fetch bars, but they can probably place paper orders, so this policy treats them
as trading keys (ADR-0008): they must never share an environment with a provider key in a
process that calls an LLM. Known gap: the daemon holds both today (`jevtrader/cli.py:84-86`),
pinned by a strict expected-failure guard test until bar fetching moves to its own child.
Live trading keys (Phase 5) are a separate inventory owned by the execution service (ADR-0001 D2).
The SEC contact is not a secret but is personal data; see [Owner identity](#owner-identity).

## Where each secret lives

| OS | Store | Status |
| --- | --- | --- |
| macOS | Keychain, generic password, service `jevtrader`, account = key name; read with `find-generic-password` (`jevtrader/secrets.py:109`), written through stdin (`jevtrader/secrets.py:120-121`) | today |
| Linux desktop | libsecret via `secret-tool`, attributes `service jevtrader account <name>`, value on stdin (`jevtrader/secrets.py:138`) | today |
| Windows | Windows Credential Manager through a fixed PowerShell script; operation, name and value on stdin (`jevtrader/secrets.py:245`) | today, untested on real Windows |
| Containers | Docker secrets mounted as files (`/run/secrets/<name lowercased>` or `JEVTRADER_SECRETS_DIR`), read-only (`jevtrader/secrets.py:286`) | today |
| Any | Process environment, for a shell or service override; the environment wins (`jevtrader/secrets.py:347-354`) | today |

`choose_store` (`jevtrader/secrets.py:310-326`) picks Docker secrets when mounted or configured, else
the platform store. The systemd user unit carries no secret: secret-looking names and values are
refused (`jevtrader/systemd.py:172`).

Every backend is tested through an injected runner only; tests never call the real Keychain,
libsecret, Windows credential tools, `launchctl`, `systemctl` or `osascript`.

## Who may read

- **Key-using commands only.** `export_to_environ` (`jevtrader/secrets.py:369-390`) runs for the
  commands in `USES_KEYS` and for `doctor`, and only with the keys that command needs: `keys_for`
  (`jevtrader/cli.py:71-89`) maps each command to its keys, and `doctor` loads only the configured
  ones (#46).
- **Child processes** get a scrubbed allowlist environment with no keys
  (`jevtrader/security/childenv.py:16-31`): `osascript`, `launchctl`, `systemctl`, `security`,
  `secret-tool` and PowerShell (#46). Trading keys: [ADR-0008](../adr/0008-trading-key-isolation.md).
- **Providers** read their one variable (`jevtrader/providers.py:186-190`).
- **Setup** asks with `getpass`, so the value is not echoed (`jevtrader/app.py:571`).
- **Never:** LLM readers, MCP clients, the web view, the notifier, coding agents, or any child
  process. Child processes get a scrubbed environment (#46).
- **Trading keys:** only the execution service, by a mechanism the Phase 5 ADR names (D2).

## Never

A secret value never appears in:

- **config** — config holds settings, never secrets (`jevtrader/config.py:1`); it is written
  owner-only (`jevtrader/config.py:81`);
- **service definitions** — launchd refuses secret-looking names (`jevtrader/launchd.py:197-209`)
  and known values (`jevtrader/launchd.py:212-216`); systemd units and Docker files follow (#28);
- **argv** — `ps` shows argv to every local process, so values go through stdin;
- **logs** and **error text** — errors name the key and the exit status, never the value;
- the **ledger** — records hold names at most;
- **prompts** — no provider request carries a key in its body, only in its auth header;
- **MCP** results — any result containing a known value is withheld
  (`jevtrader/mcp_server.py:197-199`);
- the repository — `tools/secret_scan.py` runs in CI and in the pre-commit checklist of CLAUDE.md.

## Rotation

1. Revoke the key at the provider or broker first.
2. Store the new one with `jevtrader setup` (or the platform store); the write is verified by
   reading it back (`jevtrader/secrets.py:120-121`).
3. Restart the service so no process keeps the old value in its environment.
4. Rotate immediately after any suspected leak, after a coding agent session that had access, and
   when a paper key is promoted to anything live (live keys are always new keys, ADR-0001 D2).

## Canary testing

- `tools/secret_scan.py --canary` feeds a synthetic value per rule through the scanner and fails
  if any rule misses it (`tools/secret_scan.py:94-97`).
- Target (#28): tests set synthetic canary keys through injected runners and assert the canary
  never reaches logs, config, the ledger, unit files, prompts, MCP results or error text.
- Canaries are generated at test time; real keys are never used in tests.

## Owner identity

- The SEC User-Agent is a contact the owner declares in config; there is no default. Setup
  recommends a dedicated alias (`jevtrader/config.py:125-137`).
- Tooling, tests and agents never fill it in with the owner's name or email and never fabricate
  one. One-off sec.gov checks use a browser or WebFetch, not a scripted client with an invented
  header.
- The contact belongs in config, not in `--user-agent` on argv.
