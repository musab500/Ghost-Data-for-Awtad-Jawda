# Token Usage and Cost Report — Ghost Data Still Exists

Summarizes the **final full-dataset run that produced `output.csv`** (518 cases).

## 1. Final run: model usage

| Provider | Model | Calls | Input tokens | Output tokens | Cost (USD) |
|---|---|---|---|---|---|
| — (none) | — | **0** | **0** | **0** | **$0.00** |

**The final run made no model calls.** No API key was present in the
environment (`ANTHROPIC_API_KEY` unset), so the request classifier used its
deterministic negation/intent fallback, and every other stage of the pipeline —
identity resolution, evidence parsing, state reconstruction, policy evaluation,
workflow assignment, step assembly, validation — is deterministic by design and
never calls a model.

| Metric | Value |
|---|---|
| Requests processed | 518 |
| Model calls | 0 |
| Total tokens | 0 |
| Avg tokens / request | 0 |
| Total cost | $0.00 |
| Avg cost / request | $0.00 |
| Wall-clock (full run) | ~1.2 s |

This is reported honestly: the solution is primarily deterministic, and in this
environment it reached its documented accuracy with no model spend.

## 2. Classifier LLM path (available, not used this run)

`classifier.py` has an optional LLM path that activates **only** when an API key
is present (`ANTHROPIC_API_KEY` or `CLAUDE_API_KEY`), read from the environment
only and never logged or cached. When enabled:

- **Model:** `claude-haiku-5-5` by default (overridable via
  `GHOST_CLASSIFIER_MODEL`), chosen as a low-cost model for a short
  classification task.
- **One call per unique request text.** Results are cached by
  `sha256(request_text + model + prompt_version)` in `code/.cache/`, so repeated
  texts and re-runs cost nothing.
- **Deduplication:** the 518 requests contain **375 unique request texts**, so a
  cold full run would make at most **375** calls (not 518); a warm run makes 0.
- The classifier accumulates real `input_tokens` / `output_tokens` from each
  API response (`RequestClassifier.calls/input_tokens/output_tokens`). On a run
  with a key, those counters hold the authoritative totals to paste here.

### Projection (estimate only — not an actual billed run)

Rough sizing for the classification prompt: system prompt ≈ 130 tokens, request
text ≈ 40 tokens input, JSON verdict ≈ 30 tokens output. For a cold run of 375
unique texts:

| Model | Calls | Est. input tok | Est. output tok | Est. cost* |
|---|---|---|---|---|
| claude-haiku-5-5 | 375 | ≈ 64,000 | ≈ 11,000 | **≈ $0.06–0.10** |

\* Order-of-magnitude estimate from public Haiku-class pricing; replace with the
classifier's recorded counters after a real keyed run. These are **projections,
not measured usage**, and are labelled as such so nothing in this report is
fabricated.

## 3. Multimodal (Person A, pending)

`multimodal.py` (PDF/PNG → Observations) is owned by Person A and was not present
for this run, so it contributed **0 observations and 0 tokens**. PDFs are
extracted with `pdftotext` (no model), and the 11 PNGs are intended to be read
once by a vision model with results cached to JSON; Person A will supply that
module together with a usage JSON carrying its real token counts, to be merged
into §1 here for the final submission.

## 4. Reproduction

```bash
# Deterministic run (what produced the submitted output.csv): 0 model calls
python3 code/main.py

# With the LLM classifier enabled (records real token counts):
export ANTHROPIC_API_KEY=...        # read from env only; never commit
python3 code/main.py
```

No API keys, credentials, or sensitive configuration are included in this
report or anywhere in the submission.
