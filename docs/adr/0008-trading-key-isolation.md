# ADR-0008: Trading keys live with a separate OS user that only the execution service runs as

| | |
|---|---|
| Status | **Proposed** (2026-09-30). Becomes Accepted when the owner signs off after A7 review |
| Deciders | Owner (accepts), A7 Security & Compliance (author) |
| Reviewers | A4 Execution & Risk, A6 Packaging & DevEx, A0 Lead Architect |
| Evidence | [Phase 0 audit](../audit/phase0.md) gap P0-42 (issue #46), §1.10; ADR-0001 D2, D4; invariant I-25 |
| Supersedes | nothing. Refines ADR-0001 D2, which asks the Phase 5 ADR to name this mechanism |

## Context

ADR-0001 D2 says the execution service alone holds the trading keys and the key that verifies
approval tokens, and that the Phase 5 ADR must name the mechanism that keeps them unreadable by
the core, by coding agents and by every process that hosts or calls an LLM. It lists three
candidates: a separate OS user with its own keychain, a Keychain access-control list bound to a
signed execution binary, and container secrets mounted only into the execution container.

Today every key sits in one store that any process of the user can probably read:

- macOS Keychain items are written and read with `/usr/bin/security`
  (`jevtrader/secrets.py:109`, `jevtrader/secrets.py:120-121`). Such items probably trust
  `security` itself, so a coding agent running as the user can read them without a prompt. This
  is unverified: no agent has touched the real Keychain.
- libsecret, the Windows Credential Manager and Docker secrets
  (`jevtrader/secrets.py:138`, `jevtrader/secrets.py:245`, `jevtrader/secrets.py:286`) have no
  per-application ACL at all: any same-user process can read them.

Issue #46 already narrowed the exposure without changing the store:

- `keys_for` (`jevtrader/cli.py:71-89`) exports only the keys one command needs, and `doctor`
  loads only the configured ones (`jevtrader/app.py:480-485`).
- Every child the package starts gets an allowlist environment with no keys
  (`jevtrader/security/childenv.py:16-31`).

That is enough for provider and market-data keys, which the core needs. It is not enough for
trading keys, because the core, its LLM hosts and any coding agent share one OS user.

## Options

| Option | Isolates from same-user agents | Platforms | Cost |
|---|---|---|---|
| A. Separate OS user with its own secret store | Yes: the OS enforces it; a same-user agent cannot read another user's keychain or `0600` files | macOS, Linux; Windows with a service account | An installer step that needs admin once; loopback IPC between users |
| B. Keychain ACL bound to a signed execution binary | Only if the trusted binary is not a general interpreter | macOS only | A compiled, code-signed helper; the ACL would otherwise trust `python3`, which any script can run |
| C. Container secrets mounted only into the execution container | Yes, against processes outside the container, unless they can reach the Docker socket | Docker deployments | A second container; an agent with Docker access can `docker exec` into it |

## Decision

1. **Primary: option A.** The execution service runs as a dedicated OS user
   (`_jevtrader-exec` on macOS, `jevtrader-exec` on Linux). Trading keys and the
   approval-verification key are stored only in that user's secret store (its own login keychain
   or a `0600` file under its home, read at start). The research user's store never holds them.
2. **Docker deployments: option C** as the equivalent. Trading secrets are mounted only into the
   execution container; the core container gets none, and neither container mounts the Docker
   socket.
3. **Option B is rejected for now.** The execution service is Python, so an ACL would have to
   trust the interpreter. It can be revisited if the execution service ships as a compiled,
   signed helper.
4. **Common rules for every option:**
   - The core reaches the execution service only through a loopback or Unix-socket API that
     accepts pending tickets and approval tokens, never keys.
   - Trading keys are never exported into any environment of the research user
     (`keys_for` has no trading-key entry, and a guard test keeps it so in Phase 5).
   - The execution service's children get the scrubbed environment
     (`jevtrader/security/childenv.py:16-31`).
   - Paper-only credentials follow the same path as live ones (ADR-0001 D2).

## Owner actions

These stay with the owner; no agent runs them (CLAUDE.md hard rules, owner decision 4).

- **Check the Keychain ACL** of the existing `jevtrader` items (Keychain Access, item, Access
  Control) and record whether `security` or "all applications" is trusted. Until then, assume a
  coding agent can read every key (#46).
- Create the execution OS user when Phase 5 lands, and move trading keys into its store.
- Rotate any key that was ever stored in the research user's store before it becomes a trading
  key.

## Consequences

- Phase 5 installers (A6) gain a one-time admin step; `jevtrader up` stays unprivileged for the
  research core.
- A coding agent running as the owner cannot read trading keys even if the Keychain ACL check
  fails, because they are not in the owner's store.
- Provider and market-data keys remain readable by same-user processes; this ADR accepts that
  residual risk and keeps it bounded by least-privilege export and scrubbed children.
