# Evidence labels and the gate

A replayed filing can look like a prediction when the model already read about what happened next. JEVTrader doesn't try to remove that contamination. It labels every forecast by whether it can count as evidence, and the scoreboard only counts the ones that can.

- [Evidence labels](#evidence-labels)
- [How each filing is counted](#how-each-filing-is-counted)
- [The model registry](#the-model-registry)
- [Declaring a cutoff](#declaring-a-cutoff)
- [The evidence gate](#the-evidence-gate)

## Evidence labels

Every new forecast records an `eligibility` label from the model registry. Only three labels count as evidence:

| Label | Meaning | Counts |
|---|---|---|
| `forward` | Decided live on a collected filing, at the time it was recorded; never backdated | yes |
| `no_model_knowledge` | Replay with the `rules` baseline: fixed word lists, no trained model | yes |
| `post_cutoff` | Replay of a filing dated more than 92 days after the model's published training cutoff | yes |
| `contaminated` | Replay of a filing the model may have seen in training | no |
| `unknown_cutoff` | Replay with a model whose cutoff is undisclosed (Jev) or undeclared | no |
| `adhoc_replay` | Replay of one filing picked by hand (`--event` with `--replay` or `--as-of`) | no |
| `synthetic` | Demo data | never |

Each forecast also freezes the registry facts behind its label (`eligibility_basis`: the training cutoff and whether it came from the registry or your config), and the filing page shows them.

The rules baseline's word lists were chosen by hand, so `no_model_knowledge` means no trained model saw the outcome, not that no human judgment went in.

## How each filing is counted

Each filing counts once: its first recorded forward call (else its first forward forecast). A replay never replaces a forward decision, and a filing with replays only uses the first evidence-eligible replay recorded, so re-scoring cannot swap in a better call. Forecasts recorded before labels existed keep their records; only forward ones count. A call is scored only once its outcome label was available at the scoreboard's `as_of` time, and synthetic records never count.

## The model registry

The registry (`jevtrader/registry.py`) holds each model's published training cutoff and where that fact came from:

| Key | Training cutoff | Source |
|---|---|---|
| `rules:rules-v1` | none (nothing is learned) | Fixed lexical rules in `jevtrader.providers` |
| `local:gpt-oss:120b`, `local:gpt-oss:20b` and four other spellings (`gpt-oss-120b`, `openai/gpt-oss-120b`, `gpt-oss-20b`, `openai/gpt-oss-20b`) | 2024-06-01 | OpenAI gpt-oss model card |
| `local:llama3.3:70b` | 2023-12-01 | Meta Llama 3.3 model card |
| `local:gemma3:27b` | 2024-08-01 | Google Gemma 3 model card |
| `jev:jev-1.13.0` | undisclosed | TypeSafe Jev: training cutoff not disclosed |
| any other `local`, `jev` or `openai` model | none on record | declare it in config (not possible for Jev) |

Model cards state cutoffs by month; the registry records the first of that month, and the 92-day buffer also absorbs the rest of the month and pages about earlier events that were crawled late. Names match exactly: a fine-tune or renamed build may have learned from later data, so it gets `unknown_cutoff` until you declare it. The cutoffs are what vendors publish; JEVTrader cannot check them.

## Declaring a cutoff

`model_overrides` in `config.json` declares facts the registry lacks, keyed `"provider:model"`:

```json
{"model_overrides": {
  "local:qwen3:32b": {"training_cutoff": "2024-10-01"},
  "openai:MODEL": {"usd_per_million_input_tokens": 1.25, "usd_per_million_output_tokens": 10}
}}
```

Rules for declarations:

- A declared cutoff cannot be set earlier than a published one, because that would relabel contaminated replays as evidence.
- Jev does not disclose a training cutoff, so none can be declared for it; Jev counts only when forward.
- The `rules` baseline has nothing to declare, and local models have no token price.
- Each resolved model is declared exactly; wildcards are not supported. At most 100 overrides.
- A declaration's `source` is kept, and the forecast's `eligibility_basis` records that the cutoff was declared in your config.

## The evidence gate

The gate is fixed in advance and fingerprinted. Nothing is registered with an outside party, but the scoreboard publishes `gate_sha256`, a hash of the gate's settings, so a reader can tell if thresholds moved.

| Setting | Value |
|---|---|
| Gate version | 1 |
| Minimum matured calls | 100 |
| Confidence | 90% |
| Futility upper bound | +0.10% (10 bps) |

- **What counts.** Matured `LONG`/`SHORT` calls whose labels are `forward`, `post_cutoff` or `no_model_knowledge`, one decision per filing as above. `WATCH` and `PASS` make no call.
- **Net return.** Sign × (stock return minus benchmark return from the next open to the tenth close) minus the assumed round-trip cost. Shorts also pay the assumed borrow cost.
- **Interval.** A Student-t interval on per-decision-date mean net returns (decision dates in New York time), so calls made on the same day count once.
- **Status.** `collecting` until 100 calls have matured. Then `supported` if the whole interval is above zero, `no_edge` if the whole interval is below +0.10%, and `inconclusive` otherwise (including when the calls fall on fewer than 2 decision dates).
- **Context, not a result.** The scoreboard also shows the average stock-minus-benchmark move of every matured, evidence-eligible filing, whatever was decided, and how many forecasts were excluded as not evidence.

The scoreboard is on the local page at `/scoreboard` (JSON at `/api/scoreboard.json`) and in the MCP tool `evidence_report`.

The gate has known blind spots: overlapping windows, many tries across ledgers, backfill bias and self-reported cutoffs. See [What can fool the gate](limitations.md#what-can-fool-the-gate).
