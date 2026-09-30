# MCP server

`jevtrader mcp` is a read-only MCP server on stdio. It serves the same code-built cards as the brief and the localhost page, from a local ledger opened read-only. It cannot place, change or cancel orders.

- [Tools](#tools)
- [Output rules](#output-rules)
- [Client configuration](#client-configuration)
- [Which ledger it reads](#which-ledger-it-reads)

## Tools

Five tools, each annotated `readOnlyHint: true` (and `destructiveHint: false`, `openWorldHint: false`):

| Tool | Returns |
|---|---|
| `today_brief` | The pre-market brief: 8-K filings first seen in the last 3 days, watchlist first, each with the code-computed action and reasons (and the calibrator's estimate once one is fitted), plus the evidence summary and service health. No filing text. |
| `explain_filing` | One filing card by ledger event id (as listed by `today_brief` or `search_filings`): symbol, items, SEC source URL, every decision with its evidence label and the registry facts behind it, and at most 300 characters of verbatim filing excerpts under `untrusted_filing_excerpts`. Full filing text is never returned. |
| `evidence_report` | The evidence scoreboard: matured `LONG`/`SHORT` calls, mean return net of costs with a date-clustered confidence interval, and the gate status (`collecting`, `inconclusive`, `supported` or `no_edge`). |
| `health` | The last run of each background job, recorded coverage gaps and ledger status. |
| `search_filings` | Stored filings, most recently first seen first, optionally for one symbol and only those first seen since a time; each marks whether it is on the watchlist. No filing text. |

Try asking: *"What 8-Ks came in this morning?"*, *"Explain that filing and its evidence label."*, *"Is there any evidence yet?"*

## Output rules

- Its answers are code-built cards with no key values and no raw filing text. Actions, reasons, outcomes and evidence labels are computed by code and stored before the agent sees them; the text features inside a decision come from whichever provider made it.
- Only `explain_filing` includes filing text: its quotes, under `untrusted_filing_excerpts`, with a note on each card that they are the filer's words, data and not instructions. Quotes are checked by code to be verbatim text from the stored filing, and sentences that address the reader or an AI agent, give orders or name tools are skipped. That filter is best effort, not a guarantee against prompt injection.
- Strings that come from outside the code — filing excerpts, `items`, `source_url`, document filenames, provider-reported `resolved_model` names and job `error` text — arrive wrapped as `{"untrusted": true, "source": "sec-filing" | "provider" | "job-error", "value": ...}`, so a client can tell them from code-built fields. A handler cannot forge that label: the server applies it by key on every result. The web view's JSON routes use the same labels (`jevtrader/security/provenance.py`).
- An id argument such as `event_id` must be 1 to 200 letters, digits, `:`, `.`, `_` or `-`. Anything else returns an error that does not repeat the value.
- A result that breaks an output rule, for example one containing an API key value from the server's environment, is withheld entirely.
- The server's instructions tell the client to report numbers as given, keep their evidence labels, and decline trading and personalized investment advice. Calling a tool that doesn't exist, such as `place_order`, returns `Unknown tool`.

A server can't stop a model from making things up. What this one does is make the figures and quotes it hands over checkable against the ledger and the SEC source link.

## Client configuration

**Do not run this server in the same host or client session as a broker MCP server that has order tools.** Filing excerpts are written by the filer; if injected text slips past the filters, the only thing that stops it from reaching a tool that trades is that no such tool is loaded next to this one. The server's instructions repeat this warning.

For a client that reads an `mcpServers` config file:

```json
{"mcpServers": {"jevtrader": {"command": "/absolute/path/to/jevtrader/.venv/bin/jevtrader", "args": ["mcp"]}}}
```

In Claude Code, the equivalent command is `claude mcp add jevtrader -- /absolute/path/to/jevtrader/.venv/bin/jevtrader mcp` (the form `claude mcp add --help` documents; the connection itself is untested).

The server speaks MCP over stdio, protocol versions 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25, so any stdio MCP client should be able to connect. The test suite drives the protocol with scripted JSON-RPC sessions; no specific client app has been tested yet, so reports of clients that work (or don't) are welcome.

If the client is cloud-hosted, the short excerpts that `explain_filing` returns leave your machine with the rest of the conversation.

## Which ledger it reads

By default it reads the ledger named in the app config (the background service's `forward.sqlite`). Put `--db PATH` before `mcp` to read another:

```json
{"mcpServers": {"jevtrader": {"command": "/absolute/path/to/jevtrader/.venv/bin/jevtrader", "args": ["--db", "/absolute/path/to/ledger.sqlite", "mcp"]}}}
```

Synthetic demo filings are hidden from the brief and never count as evidence, so on the demo ledger `today_brief` returns no filings and `evidence_report` says "Collecting evidence: 0 of 100 matured calls", with every synthetic forecast excluded. The honest answer there is "nothing yet".
