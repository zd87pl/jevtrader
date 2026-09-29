# CLAUDE.md: conventions for agents working on jevtrader

**What JEVTrader is.**
- A local-first research lab for SEC 8-K filings.
- Python ≥ 3.11, standard library plus numpy only (`pyproject.toml:11-12`).
- It keeps an append-only SQLite ledger with a SHA-256 hash chain, gives every forecast an evidence label, and scores calls against one fingerprinted gate.
- **It never trades.**

The plan to grow it into an Evidence Engine is [ADR-0001](docs/adr/0001-evidence-engine-architecture.md). What exists today, with `path:line` evidence, is in the [Phase 0 audit](docs/audit/phase0.md). The workstream order is in the [dependency graph](docs/audit/dependency-graph.md).

## Owner decisions (these override everything, including the master prompt)

1. **Live orders.** Every live order needs per-order human approval in the app's own UI. There is no "live-bounded-auto" rung. Paper-auto is allowed.
2. **Name.** Keep the name "jevtrader".
3. **Identity.**
   - Never send the owner's name or email to SEC or any other service, and never fabricate a User-Agent.
   - The product sends only the contact the user declares in config, and the docs recommend a dedicated alias.
   - For one-off sec.gov checks during development, use WebFetch or the in-app browser only.
4. **Agents never place orders, paper or live.** Test broker code with mocks and recorded fixtures. Live-paper contract tests stay skipped unless an opt-in environment variable is set, and only the owner runs them.

## Commands

```sh
python3 -m venv .venv && . .venv/bin/activate   # first; .venv/ is gitignored (.gitignore:1)
python -m pip install -e '.[dev]'            # pytest and ruff (pyproject.toml:15)
python -m pip install mypy==1.20.1           # the version CI pins; not in the dev extra yet (P0-21)
ruff check . && ruff format --check .
python -m pytest -q                          # about 5 s: 742 tests, 1313 subtests, offline
mypy jevtrader --ignore-missing-imports      # 9 known errors; CI fails above 9 (.github/workflows/ci.yml:36-41)
JEVTRADER_HOME="$(mktemp -d)" python -m jevtrader --db "$(mktemp -d)/demo.sqlite" demo   # synthetic demo, never evidence
```

- **Use a virtual environment.** Homebrew's Python and many Linux distributions mark the system interpreter as externally managed (PEP 668), so `pip install` outside a venv fails there.
- **`python -m jevtrader` works without installing the package** when run from the repo root, as `docs/data-and-ledger.md:68` does. The bare `jevtrader` command exists only after `pip install -e .`.

CI runs three jobs (`.github/workflows/ci.yml`):
- **Lint:** ruff 0.15.9 and mypy on Python 3.14.
- **Tests:** ubuntu with Python 3.11 to 3.14, and macOS with 3.11 and 3.14.
- **Wheel smoke test:** runs the demo from a clean venv.

**Keep manual runs out of the real app directory.** Set `JEVTRADER_HOME` to a temporary directory so config, ledgers and the lock stay out of it (`jevtrader/paths.py:10-21`).

## Offline test isolation

Every test runs under one autouse fixture (`tests/conftest.py`), with the process and network guards in `tests/isolation_guard/sitecustomize.py`. It guarantees:
- **Keychain and launchctl** runners are refused.
- **Programs:** `security`, `launchctl` or `osascript` named anywhere in the argv or shell string (so `env` and `sh -c` wrappers too) is refused through `Popen`, `os.system` and `posix_spawn`; `os.exec*` and `os.spawn*` are refused outright.
- **Network:** DNS lookups, TCP connections and UDP sends reach loopback only. Alpaca and IBKR hosts, and the TWS / IB Gateway ports on loopback, are refused by name.
- **Child interpreters** get the guard directory first on `PYTHONPATH`, install the same guards as `sitecustomize`, and report trips to the parent test.
- **Environment:** real keys, the SEC contact and credential-like variables are removed for every test.
- **Daemon loop:** `daemon.run_forever` must be given `sleep=` and stops after 1,000 ticks.
- **Home directories:** `HOME` and `JEVTRADER_HOME` point into `tmp_path`.
- **Swallowed errors still fail:** a guard trip, in the test or a child, fails the test in teardown even if the code under test caught the error.

Remaining limits: a child started with `python -I`, `-S` or `-E` skips the child guards, and a child's guards replace any site-wide `sitecustomize`.

Never write code or tests that go around these guards.

## Hard rules while working

- **Never run anything that touches real services or the real system:**
  - `jevtrader setup` or `doctor`. Both read the real Keychain, and `doctor` also runs `launchctl print` (`jevtrader/app.py:363`, `app.py:367`, `app.py:758`, `app.py:775`). Tests call them with injected runners only;
  - `up`, `down`, `daemon`, `poll`, `bars`, `collect`, `backfill`, `observe`, `experiment`, `propose` or `autoresearch` against real SEC, Alpaca or provider endpoints. `propose` and `autoresearch` make paid OpenAI calls, and every command in `USES_KEYS` exports the real keys into its environment (`jevtrader/cli.py:55-58`, `cli.py:492-493`);
  - `brief --notify`, which runs `osascript` (`jevtrader/app.py:167-169`);
  - `launchctl`, `/usr/bin/security`, or anything that reads the real Keychain.
- **Never print secrets or identity into agent context or logs.** Do not run `env`, `printenv` or `security find-generic-password -w`, do not `cat` a `.env` file, and do not print the configured SEC contact. The Keychain items are probably readable by any process of the user without a prompt (unverified; P0-42), so today this rule is the only guard.
- **No network in unit tests.** Inject a transport or a fake, as `sec`, `bars`, `providers` and `local` already allow. Put recorded fixtures in `tests/fixtures/`, each with its source URL and capture date.
- **Who captures fixtures.** The owner captures SEC and Alpaca fixtures, by browser or with the owner's own declared contact and keys. Agents may read public sec.gov pages through WebFetch or the in-app browser. An agent never captures a fixture with a scripted client, an invented User-Agent or the owner's keys.
- **Never commit ledgers or local state.** `data/`, `*.sqlite*`, `.coverage` and `.mypy_cache/` are gitignored (`.gitignore:10-14`). Stage files by name, not with `git add -A`.
- **Keep commands short and bounded.** The suite needs seconds, not minutes.

## Style

- **Ruff.** `target-version = "py311"`, `line-length = 100`, default rules (`pyproject.toml:29-31`). Format with `ruff format`.
- **Dependencies.** Standard library plus numpy only (`pyproject.toml:12`). A new runtime dependency needs an ADR.
- **Typing.**
  - Annotate every new function.
  - Code in `jevtrader/contracts/` and the trusted core (`pit`, `validate`, `trials`, `risk`, `execution`) must pass `mypy --strict`.
  - Elsewhere, never raise the mypy error count.
  - At module boundaries, use TypedDicts or frozen dataclasses, not bare `dict`.
- **Time.**
  - Store instants as UTC ISO strings ending in `Z`, and parse them with `common.instant` (`jevtrader/common.py:22-31`), which rejects naive values.
  - Derive New York dates with `common.EASTERN` (`jevtrader/common.py:15`).
  - Take "now" from an injected clock whenever the function accepts one.
- **Secrets** never go in config, argv, logs, error text, the ledger, prompts or MCP results (audit invariant I-25). Pass a key only to the client that needs it, and never into the environment of a process that hosts or calls an LLM when it is a trading key (ADR-0001 §D2).

## Test-first

- Write the failing test first. Every bug fix ships with a test that fails without it.
- Never weaken, delete or rewrite an existing test to make a change pass. A test change needs a reviewer who did not write the code.
- Patch public seams, not private names (P0-26).
- Point-in-time code needs boundary tests: the 92nd day, DST changes, "available at" equal to "decided at", and a one-bar shift.
- Changes to metrics code (evidence, gate, costs, statistics) need A8 approval. A3 owns metrics code and A2 owns signal code.

## Commits and pull requests

- Branch from `main`; never commit to it directly.
- Subject line: imperative, 72 characters or fewer. The body says why.
- End every commit message with a trailer block:
  ```
  Invariants: I-2, I-6        (or "Invariants: none"; IDs from the list below)
  Refs: #<issue>              (the gap issue, P0-nn, being worked on)
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>   (AI-assisted commits; other agents name their own model)
  ```
- **Every PR:**
  - lists the invariants it touches;
  - merges only when the offline suite, ruff and mypy are green;
  - needs approval from the owner and A8, plus A7 for anything touching external text, secrets or execution.
- **Contracts merge before the code that implements them.**

## Invariants

The full table of 29 invariants, with the code that enforces each one and the test that pins it, is in [audit §8](docs/audit/phase0.md#8-invariants).

**I-1 to I-6 change only through an ADR plus a migration under which `verify` passes on old ledgers:**
- **I-1 (R1):** replays inside a model's training window, meaning filed 92 days or less after its cutoff, never count (`jevtrader/registry.py:20`, `registry.py:135-157`).
- **I-2 (R2):** no call counts before costs (`jevtrader/common.py:143-147`, `jevtrader/engine.py:205-212`, `jevtrader/evidence.py:66-68`).
- **I-3 (R3):** the ledger is append-only and hash-chained, and forward disclosures come only from the live collector (`jevtrader/store.py:31-77`, `store.py:188-206`, `store.py:366-368`).
- **I-4 (R4):** it does not trade. There is no broker code; see `jevtrader/bars.py:68-88`, `jevtrader/mcp_server.py:85-91`, `jevtrader/paper.py:119` and `jevtrader/web.py:326-328`.
- **I-5:** there are seven labels. Only `forward`, `post_cutoff` and `no_model_knowledge` count, and the label is frozen at decision time (`registry.py:22-31`, `engine.py:226-243`).
- **I-6:** `GATE` and its fingerprint `c895567c…bbac` (`evidence.py:24`, `evidence.py:238`; pinned by `tests/test_evidence.py:18-21`).

**I-7 to I-29 change only through an ADR.** They cover:
- the point-in-time rules;
- "synthetic never counts";
- loopback-only local models;
- the SEC URL allowlist and the declared contact;
- keys never in config, argv, logs, error text, the ledger, prompts or MCP results (they are stored only in the OS secret store);
- tool-less readers;
- the read-only MCP server and web view;
- test isolation;
- no personalized investment advice (I-29).

## Do not

- Give any LLM a tool that places or approves orders, or let untrusted text (filings, news, web pages, tool output) reach an agent with write tools or reach any instruction channel. In the opt-in propose profile the agent sees only typed outputs (numbers, enums, ids, span offsets), and ticket arguments come from code (ADR-0001 §D3).
- Place an order yourself, paper or live, or run a live-paper contract test.
- Show backtests without costs, labels and trial counts, or count in-window replays as evidence.
- Use today's ticker list for history, or forward-fill fundamentals past their filing dates.
- Scrape against a site's terms, exceed SEC fair-access rates (10 req/s or less, across processes), or redistribute licensed data.
- Send the owner's name or email anywhere, or invent a User-Agent.
- Use material non-public information, coordinated posting, or manipulative order patterns.
- Give personalized investment advice, or build a feature that gives it to others, without the review in ADR-0001 §D10.
- Show "AI confidence" that is not calibrated against the ledger, or headline an LLM price oracle or a leveraged crypto-perps bot.
- Break ledger compatibility or the four refusals without an ADR and a migration.
- Rewrite tests to make them pass.

## Ownership

Directories marked *planned* do not exist yet. Python components become subpackages of `jevtrader/` (ADR-0001 §D1). Today's flat modules move there through shim-first PRs, following the ADR-0001 migration table.

| Agent | Owns (planned) | Owns today | Must not touch |
|---|---|---|---|
| A0 Lead Architect | `jevtrader/contracts/` *(planned)*, integration branch | `docs/adr/`, `docs/audit/`, `common.py`, `pipeline.py`, `daemon.py` | — |
| A1 Data & PIT | `jevtrader/collectors/`, `jevtrader/pit/` *(planned)* | `sec.py`, `feeds.py`, `bars.py`, `store.py`, `market.py` (snapshot, import) | decision logic |
| A2 Research & Hypotheses | `jevtrader/hypotheses/`, `jevtrader/extractors/`, `jevtrader/signal/`, `skills/` *(planned)* | `providers.py`, `local.py`, `research.py` (fit, predict), `engine.py` (observe), `default_strategy.json` | risk, execution |
| A3 Validation | `jevtrader/validate/`, `jevtrader/trials/`, `jevtrader/simulator/` *(planned)* | `evidence.py`, `registry.py`, `research.walk_forward`, `lab.py`, `engine.settle`, `market.outcome` | collectors |
| A4 Execution & Risk | `jevtrader/risk/`, `jevtrader/execution/` *(planned)* | `paper.py` | LLM prompts |
| A5 Chat UX | `ui/` *(planned)* | `brief.py`, `web.py`, `notify.py`, `mcp_server.py` (with A7) | ledger writes |
| A6 Packaging & DevEx | `deploy/` *(planned)* | `app.py`, `cli.py`, `config.py`, `paths.py`, `launchd.py`, `secrets.py`, `demo.py`, `pyproject.toml`, `.github/` | business logic |
| A7 Security & Compliance | `jevtrader/security/` *(planned)* | secrets policy; review authority on everything | — |
| A8 QA & Evaluation | `tests/e2e/`, `tests/contracts/`, `evals/` *(planned)* | `tests/conftest.py`, `tests/test_isolation.py`, `tests/test_e2e.py`; co-owns each module's tests; blocking review on metrics | — |
