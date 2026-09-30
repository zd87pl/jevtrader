# Threat model

Status: living document, Phase 1c. It describes the code as of this branch and the target in
[ADR-0001](../adr/0001-evidence-engine-architecture.md) (D2 execution, D3 untrusted text, D4
secrets, D8 read-only surfaces). Every `path:line` citation is checked by
`tests/test_security_docs.py`, so a citation that drifts fails CI. The secrets rules are in
[secrets-policy.md](secrets-policy.md).

## Assets

| Asset | Why it matters | Where it lives today |
| --- | --- | --- |
| Broker keys (`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`) | Paper account keys today; can probably place paper orders (ADR-0001 D2). Live keys later. | Keychain or environment (`jevtrader/secrets.py:23`) |
| Provider keys (`TYPESAFE_API_KEY`, `OPENAI_API_KEY`) | Billable; a leak spends money. | Same store (`jevtrader/secrets.py:23`) |
| The ledger | System of record (R3); poisoned rows bias every later gate. | SQLite under the data directory (`docs/data-and-ledger.md`) |
| Prompt templates and question sets | Trusted instruction channel of every extraction. | Code plus strategy JSON (`jevtrader/providers.py:282-302`) |
| The owner's SEC contact | Personal data (O3); must never be fabricated or leaked. | Config only (`jevtrader/config.py:125-137`) |
| The user's host session | An assistant reading our output may hold broker or shell tools. | MCP client, browser |

## Trust zones

1. **Owner zone (trusted).** The owner's shell, config (`jevtrader/config.py:1`), strategy files
   and approved prompt templates.
2. **Core process (trusted code, untrusted data).** Collectors, readers, the calibrator, the
   ledger. It holds provider and market-data keys in its environment after
   `export_to_environ` (`jevtrader/secrets.py:369-390`).
3. **Quarantined readers (untrusted output).** Provider LLMs and the local reader. They receive
   filing text and return bounded values; their output is data, never instructions.
4. **Read-only surfaces (untrusted consumers).** The MCP server over stdio
   (`jevtrader/mcp_server.py:525-536`) and the loopback web view (`jevtrader/web.py:29`). Their
   consumers, host LLMs and browsers, are outside our control.
5. **External (untrusted).** SEC EDGAR, Alpaca, text providers, and every filer.
6. **Execution service (Phase 5, not built).** The only holder of trading keys (ADR-0001 D2).

## Actors

- **Malicious filer.** Anyone who can file an 8-K controls its text, HTML, exhibits, filenames
  and timing. Goals: steer an extraction (poisoning), get an instruction quoted to an assistant
  (prompt injection), hide text from a human reviewer (hidden HTML, bidi, zero-width).
- **Prompt injection via any external text.** Filing text, provider error bodies, provider model
  names and URLs can all carry instructions aimed at a reader LLM or at a host LLM that reads our
  MCP or web output.
- **Autoresearch LLM.** Writes questions that become instructions; they stay drafts until a person approves the diff (`jevtrader/lab.py:197-209`);
  compromised or merely wrong, it can drift the instruction channel.
- **Co-installed agent or local process.** Runs as the same user: a coding agent, another MCP
  server (possibly a broker server with order tools), a browser tab doing DNS rebinding.
- **Network attacker.** On-path to SEC, Alpaca or a provider; TLS is the boundary.
- **Compromised provider.** Returns hostile payloads or redirects.
- **The owner, by mistake.** Pastes a key into config, argv or a chat.

## Entry points

| # | Entry point | Code | Input trust |
| --- | --- | --- | --- |
| E1 | SEC fetches, allowlisted HTTPS URLs only | `jevtrader/sec.py:177-196` | untrusted |
| E2 | Filing HTML to text through the central sanitizer (`sanitize-v1`): hidden tags (`script`, `style`, `noscript`, `head`, `template`, `ix:hidden`) and `display:none`/`visibility:hidden` dropped, NFKC, format and bidi characters stripped | `jevtrader/security/sanitize.py:34` | untrusted until sanitized |
| E3 | Provider prompts: fixed instructions and questions in the instruction channel; filing text only in hash-tagged data blocks | `jevtrader/providers.py:282-302`, `jevtrader/providers.py:411-419` | trusted template, untrusted blocks |
| E4 | Provider HTTP with redirects refused | `jevtrader/providers.py:203-207` | untrusted response |
| E5 | Alpaca bars with key headers | `jevtrader/bars.py:177-178` | untrusted response |
| E6 | Autoresearch proposals, run as experiments only after a person approves the question diff | `jevtrader/lab.py:197-209` | LLM-written |
| E7 | Brief cards quoting filing sentences, directive filter | `jevtrader/security/quarantine.py:28` | untrusted |
| E8 | MCP stdio server | `jevtrader/mcp_server.py:525-536`, `jevtrader/mcp_server.py:41` | untrusted client |
| E9 | Web view, loopback, GET/HEAD only | `jevtrader/web.py:323-328`, `jevtrader/web.py:417-418` | untrusted client |
| E10 | Local reader, loopback endpoints only | `jevtrader/local.py:45` | local |
| E11 | Keychain reads and writes | `jevtrader/secrets.py:120-121`, `jevtrader/secrets.py:369-390` | owner |
| E12 | Key export for key-using commands and doctor | `jevtrader/cli.py:66-68`, `jevtrader/cli.py:574-576`, `jevtrader/app.py:480-485` | owner |
| E13 | Notifier child process | `jevtrader/notify.py:19` | our text, scrubbed env |

## Threats

STRIDE categories: S spoofing, T tampering, R repudiation, I information disclosure, D denial
of service, E elevation of privilege.

| Id | STRIDE | Threat | Entry |
| --- | --- | --- | --- |
| T1 | T, E | A malicious filer hides instructions from the human reader but not from an LLM reader: CSS hiding the sanitizer does not recognise (CSS escapes, tiny but non-zero fonts, clipping, text coloured like a non-white background) or invisible characters outside its strip list, and a reader follows them. | E2, E3 |
| T2 | T | Adversarial but in-range extraction values poison the ledger and the calibrator (audit §5.2). | E3 |
| T3 | E | Prompt injection in a quoted excerpt reaches a host LLM that also holds a broker MCP server, and it places an order. | E7, E8 |
| T4 | E, T | Autoresearch writes questions that become instruction text without human review. | E6, E3 |
| T5 | I | A key reaches argv, a plist, logs, error text, the ledger, a prompt or an MCP result. | E11, E8 |
| T6 | I, E | Any same-user process reads Keychain items written through `/usr/bin/security`, or a child inherits every exported key. | E11, E12, E13 |
| T7 | S, I | DNS rebinding or a cross-site request reaches the web view. | E9 |
| T8 | S, T | A redirect forwards credentials or repeats a billable POST. | E4 |
| T9 | T | A crafted URL or query steers a fetch to an unapproved host. | E1 |
| T10 | D | Oversized filings or MCP results exhaust memory or the host context. | E2, E8 |
| T11 | R | A prompt or template change cannot be tied to the records it produced. | E3 |
| T12 | I | The owner's identity is sent to SEC or fabricated in a User-Agent. | E1 |

## Mitigations

| Threat | In place (cited) | Planned (issue) |
| --- | --- | --- |
| T1 | Central versioned sanitizer (`jevtrader/security/sanitize.py:46-48`) on collection, and again in the engine before any provider call for legacy ledger text. `sanitize-v2` drops `display:none`, `visibility:hidden`, `font-size:0`, `opacity:0`, off-screen offsets and zero-height `overflow:hidden` after removing CSS comments (`jevtrader/security/sanitize.py:54-64`), and strips private-use, unassigned and default-ignorable characters (`jevtrader/security/sanitize.py:120-125`). Concealed body text and near-white text quarantine the filing; structural drops such as `head` or the iXBRL header do not (`jevtrader/security/quarantine.py:136-142`). Quarantine matches a narrow directive pattern (AI-reader references, override phrasing, role markers, tool calls and names, sentence-initial buy/sell), not the broad quote filter, so 8-K boilerplate still counts (`jevtrader/security/quarantine.py:51`) | Done in #9 and #13 (red-team fix-ups RT-1 to RT-3); display surfaces still use their own filters (#12) |
| T2 | Readers have no tools and return bounded numbers; adversarial-looking source text flags the extraction quarantined (`jevtrader/engine.py:192`), and a quarantined forecast never counts (`jevtrader/evidence.py:49`), trains (`jevtrader/engine.py:394`) or gets a paper plan (`jevtrader/paper.py:131`); red-team corpus in CI (`tests/redteam/cases.json`, `tests/test_redteam.py`) | Done in #13; paraphrased directives can still pass unflagged |
| T3 | Directive sentences dropped from cards, matched on the confusable skeleton (`jevtrader/security/quarantine.py:28`); excerpts only from `explain_filing` under an untrusted label (`jevtrader/mcp_server.py:41`); external-derived MCP fields wrapped with provenance (`jevtrader/mcp_server.py:50`); id arguments bounded and never echoed (`jevtrader/mcp_server.py:225`); co-installed broker warning in `docs/mcp.md` and the server instructions | Paraphrased directives; `/api/brief.json` fields still unlabelled (escaped in HTML only) |
| T4 | Proposals pass validation and a question lint, and run only after human approval of the diff (`jevtrader/lab.py:197-209`) | Done in #10 |
| T5 | Values go through stdin, never argv (`jevtrader/secrets.py:120-121`); MCP withholds any result containing a key (`jevtrader/mcp_server.py:197-199`, `jevtrader/mcp_server.py:313-318`); `tools/secret_scan.py` | Canary secrets across every sink: #28 |
| T6 | Only key-using commands export, and only their needed keys (`jevtrader/cli.py:71-89`); children get a scrubbed env (`jevtrader/security/childenv.py:16-31`) | Execution-only trading keys (ADR-0008) and the owner's ACL check: #46 |
| T7 | Loopback bind only (`jevtrader/web.py:417-418`), Host check and read-only methods (`jevtrader/web.py:323-328`) | Auth and CSRF on any future state-changing route (ADR-0001 D2) |
| T8 | Redirects refused (`jevtrader/providers.py:203-207`) | none |
| T9 | Approved-URL allowlist (`jevtrader/sec.py:177-196`); local reader loopback only (`jevtrader/local.py:45`) | none |
| T10 | MCP result and text size caps (`jevtrader/mcp_server.py:313-318`) | Raw store with bounded parsing: #9 |
| T11 | Instructions are code constants (`jevtrader/providers.py:282-302`) | Done in #11: separate channels, hash-tagged blocks, template hash in the spec and extractor key, exact request stored in the ledger |
| T12 | Contact is required config, alias recommended (`jevtrader/config.py:125-137`) | Owner-identity rule in [secrets-policy.md](secrets-policy.md#owner-identity) |

Cross-platform storage for every mitigation above is #28; broker-key isolation is #46.

## Residual risks

- **Keychain ACL unverified.** Items written by `/usr/bin/security` may be readable by any
  same-user process without a prompt. Until the owner checks the ACL (#46), assume a coding
  agent can read every key.
- **Environment inheritance.** Fixed for the package's own children: each gets a scrubbed
  allowlist environment (`jevtrader/security/childenv.py:16-31`), including the notifier's
  `osascript` (`jevtrader/notify.py:19`). A command still holds its own needed keys in
  `os.environ`; trading keys move out of reach in Phase 5 (ADR-0008, #46).
- **Legacy ledger text is unsanitized** and cannot be re-sanitized for hidden HTML, since no raw
  store exists (ADR-0001 D3, #9).
- **Sanitizer residual (T1).** CSS escapes (`\64 isplay`), tiny non-zero fonts, `clip`,
  stylesheet rules in `<style>` that hide a class, and text coloured like a dark background stay
  in the text unflagged. Near-white text is kept (the background is unknown) but quarantines the
  filing. Mitigations: the E2 sanitizer, quarantine, and the directive filter on quotes.
- **The directive filter is best effort.** A paraphrased instruction can survive
  (`jevtrader/security/quarantine.py:28`); the host LLM remains the last line (#12).
- **Co-installed broker MCP servers** are outside our control; we can only warn (#12).
- **Paper keys double as market-data keys** and can probably place paper orders (ADR-0001 D2).
- **Provider-side retention** of filing text and prompts is governed by provider terms, not code.
- **Per-process SEC rate limit**; several processes can exceed the global limit (ADR-0001 D4).
