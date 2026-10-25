# Token Usage and Cost Report — Ghost Data Still Exists

Summarizes the final full-dataset run that produced `output.csv` (518 requests). All figures are read from recorded API responses; nothing here is estimated.

## 1. How the final run used models

- Classifier mode in the final run: **fallback** (`claude-haiku-5-5`).
- New classifier API calls in that run: 0; served from the on-disk cache: 0; failures: 0.
- Multimodal observations loaded: 0.
- Every other stage (identity, parsing, state, policy, steps, validation) is deterministic and makes no model call.
- Cached calls were paid for once, when the cache was populated; their recorded usage is reported below.

## 2. Usage and cost per model

No model calls were recorded. Overall: 0 calls, 0 tokens, $0.00.

## 3. Per-request averages (÷ 518 requests)

- Total tokens per request: 0.00
- Input tokens per request: 0.00; output tokens per request: 0.00
- Cost per request: $0.00000000

## 4. Prices used

Source: https://platform.claude.com/docs/en/about-claude/pricing; retrieved 2026-10-25; USD per million tokens (MTok), base input and output; prompts up to 100,000 tokens.

- `claude-haiku-5-5`: $0.1 input / $0.5 output per MTok
- `claude-sonnet-5-5`: $2.0 input / $10.0 output per MTok
- `claude-opus-5-5`: $4.0 input / $20.0 output per MTok

## 5. Notes

- Request classifier: no usage file found, so **0 recorded calls**.
- Multimodal (images/PDFs): no usage file found, so **0 recorded calls**.

No API keys or credentials appear in this report.
