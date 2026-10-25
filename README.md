# Ghost Data Still Exists — Deletion-Audit Agent

HackerRank Orchestrate (October 2026). The full task statement is in
[`problem_statement.md`](./problem_statement.md).

## What the agent does

For every request in `dataset/deletion_requests.csv` (518 cases) the agent:

1. decides whether the person actually asked for deletion (a data copy, a
   correction, a marketing opt-out or login help is `NOT_APPLICABLE`);
2. resolves the person's stable `user_id` and every current and historical
   identifier (emails, phones, email hashes);
3. finds every record that mentions them in the supplied evidence (SQL
   snapshots, JSON, logs, text, PDFs, screenshots) and the activity stream;
4. reconstructs each record's state **at the case's `audit_at`**, ignoring later
   evidence and treating `reported_status` and activity results as claims;
5. applies `deletion_rules.csv` (delete, redact, anonymize, retain, tombstone,
   plus active data flows and restorable backups);
6. returns `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` with the ordered
   findings, remediation plan and verification checks, and writes `output.csv`.

## Architecture

```
deletion_requests.csv ─► classifier.py ── not a deletion ──► NOT_APPLICABLE, []
                          (LLM, optional)                     
                          + negation override                
                          + deterministic fallback
                                │ deletion
                                ▼
 user_identities.csv ─►  identity.py ─► Identity (emails, phones, hashes,
 user_audit_log.csv                      sha256(lower(email)) of every email)
                                │
 dataset/evidence/* ─► parsers/* (+ multimodal.py: PDF/PNG ─► Observations)
 system_activity.csv ─►          │
                                 ▼
                         state.EvidenceIndex
                           link_requests(all requests)  resources per request window, per person
                           assign_workflow(all)     identifier-less resources, matched globally
                           state_at(resource, audit_at)
                                 │
                                 ▼
                         rules.decide(state)  ─►  finding / remediation / verification
                                 │
                                 ▼
                         steps.assemble_steps ─► ordered steps_json
                                 │
                                 ▼
                         main.write_output ─► output.csv ─► validate_output_file
```

### Where a model is used, and why the rest is deterministic

| Stage | Method | Reason |
|---|---|---|
| Request classification | LLM through `llm_provider.py` (`classifier.py`) with a negation override and a deterministic fallback | Requests are free text in about ten languages and keyword matching misses negation |
| Images (PNG) | **Manual transcriptions verified against the images** (`code/multimodal_manual/`, committed). A vision model (`llm_provider.vision_json`, `multimodal.py`) was used as a draft and cross-check; its results are compared per file and used only where verified | Screenshots have no text layer. PDFs are read with `pdftotext`, no model |
| Identity, parsing, temporal state, policy, ordering, validation | Plain Python | Exact matching, timestamps and rule application must be reproducible and auditable |

The model never decides state, policy or step order.

### Screenshot observations: what is committed and how the vision model was used

The 11 PNG screenshots are turned into observations from **manual transcriptions that were checked against the
images** (`code/multimodal_manual/`, committed, no API call). A vision model (Gemini, through `llm_provider.py`) was
run on the same screenshots as a **draft and cross-check**: every VLM result was compared with the manual
transcription and each difference was judged by opening the image. A VLM result enters the pipeline only if it
matches the image (frozen in `code/multimodal_vlm/`); otherwise the manual transcription is used. The runs, models,
prompt versions, tokens, per-file agreement and the rejected files are in
`code/evaluation/evaluation_report.md` (section 5) and `code/evaluation/usage_report.md` (section 6);
the reasoning is in `code/parsers/NOTES.md`. A live vision call needs an explicit opt-in
(`MULTIMODAL_LIVE=1`, or `python3 tools/compare_multimodal.py --run`), so a normal run never spends model quota on images.

### Modules

| Path | Purpose |
|---|---|
| `code/main.py` | Pipeline, `output.csv` writer, output validator |
| `code/identity.py` | Identity resolution |
| `code/llm_provider.py` | Gemini / Anthropic behind `complete_json` and `vision_json`; validation, retries, real usage |
| `code/classifier.py` | Deletion classifier, cache, usage log |
| `code/steps.py` | Step assembly and ordering |
| `code/models.py`, `code/parsers/*`, `code/state.py`, `code/rules.py` | Evidence model, parsers, state at `audit_at`, policy engine |
| `code/evaluation/main.py` | Sample evaluation and custom cases (`custom_cases/`) |
| `code/evaluation/classifier_eval.py` | LLM vs fallback vs combined classifier report |
| `code/evaluation/usage_report.py` | Builds `usage_report.md` from recorded API usage |
| `code/tests/` | Unit tests for parsers, state and rules |

## Setup

- Python 3.10 or newer (developed on 3.13). The pipeline uses only the standard library.
- Optional, for the LLM classifier and the vision step: `pip install -r requirements.txt` (installs `google-genai` and `anthropic`). Without them the pipeline runs in deterministic fallback mode and the tests that need an SDK are skipped.
- Tests use the standard-library `unittest`; nothing else to install.
- `pdftotext` (poppler-utils) is used for PDFs when multimodal extraction runs.

### LLM provider and API key (optional, required for a real LLM run)

The request classifier and the vision step call a model only when a provider key is present.
Without one the pipeline still runs end to end in **fallback mode** (deterministic rules, zero model
calls) and prints a `NOTICE: ... FALLBACK mode` line to stderr. The default provider is **Google
Gemini**; Anthropic works through the same interface (`code/llm_provider.py`).

1. Install the SDKs: `pip install -r requirements.txt` (`google-genai`, `anthropic`).
2. Create a file named `.env` in the repository root (it is in `.gitignore`; never commit it and
   never paste a key into a chat, a log or a report):

   ```
   GEMINI_API_KEY=your-key-here
   ```

   On PowerShell you can set it for the current window only instead: `$env:GEMINI_API_KEY = "your-key-here"`.
3. Check the setup. This prints only `key present: yes/no` per provider and the provider and model
   names that WILL be used, never a key:

   ```bash
   python3 tools/check_env.py            # configuration only, no network
   python3 tools/check_env.py --list     # Flash model names your key can see (pick CLASSIFIER_MODEL / VISION_MODEL)
   python3 tools/check_env.py --ping     # one tiny real call per model: confirms each model name exists
   ```

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Gemini Developer API key |
| `ANTHROPIC_API_KEY` (or `CLAUDE_API_KEY`) | Anthropic key |
| `LLM_PROVIDER` | `gemini`, `anthropic` or `none`. Default: `gemini` if its key is set, else `anthropic` if its key is set, else `none` (fallback mode) |
| `CLASSIFIER_MODEL` | Model for classification. Default: `gemini-3.5-flash-lite` (Gemini) or `claude-haiku-5-5` (Anthropic) |
| `VISION_MODEL` | Model for screenshots. Default: `gemini-3.6-flash` (Gemini) or `claude-haiku-5-5` (Anthropic) |

The default model names come from the google-genai SDK's enumerated model list (`gemini-3.5-flash-lite` and `gemini-3.6-flash`, the two models in `prices.json`). They were **not
confirmed by a live call** where this was written (no key was available), so run
`python3 tools/check_env.py --ping` once with your key, or set `CLASSIFIER_MODEL` / `VISION_MODEL`.

**Results are cached.** Each classifier verdict is stored in `code/.cache/classifier.json`, keyed by
`sha256(request text + provider + model + prompt version)`, so every unique text is sent at most once
and re-runs are free. A blocked, empty, truncated or malformed response is never cached: that text
uses the deterministic rules for the run and is retried on the next one. Real token usage from each
API response (Gemini thinking tokens counted as output) goes to `code/.cache/classifier_usage.json`.
Prices live in `code/evaluation/prices.json` (source URL and retrieval date per model); set `gemini_tier`
there to `free` or `paid` (free tier reports $0 with the paid price as a reference). A truncated answer
(`max_output_tokens` hit) is retried once with a higher limit and never parsed or cached; each
response, including the retry, is its own record in the usage file.

## Commands

Run everything from the repository root.

```bash
python3 code/main.py                          # writes ./output.csv (518 rows) and validates it
python3 code/main.py --validate-only output.csv
python3 code/evaluation/main.py               # samples + custom cases; WRITES evaluation_report.md from the run
python3 code/evaluation/classifier_eval.py    # needs a key; writes classifier_comparison.md (included in the report)
python3 code/evaluation/usage_report.py       # WRITES usage_report.md from recorded API usage and prices.json
python3 -m unittest discover -s code/tests
python3 tools/check_env.py                    # key present yes/no and model names (never the key)
python3 tools/final_run.py                    # tests, main, evaluation, usage, strict validation; non-zero on any failure
bash tools/package.sh                         # builds and verifies code.zip (needs bash and zip)
python3 tools/merge_logs.py a.txt b.txt --out merged_log.txt   # one chronological log; --check to verify only
```

## Outputs and caching

- `output.csv` (repository root): `case_id,deletion_status,steps_json`, sorted by `case_id`.
- `code/evaluation/evaluation_report.md` and `code/evaluation/usage_report.md`: **generated** by the scripts above. The narrative of the evaluation report is in `code/evaluation/report_template.md`; edit that, never the generated file.
- `code/.cache/` (gitignored, created at run time):
  - `classifier.json`: raw LLM verdicts keyed by `sha256(text + model + prompt version)`, so each unique request text is sent once;
  - `classifier_usage.json`: one record per real API call (model, input and output tokens);
  - `last_run.json`: how the latest `main.py` run classified requests.

`main.py` validates its own output and fails loudly on any contract violation: header, row count,
sort order, JSON, consecutive `step_no`, phase and operation order, evidence citation rules. A `COMPLETE`
row must contain a verification step and every step must cite evidence. Until multimodal evidence is
loaded these two checks print warnings instead of failing; once it is loaded they are errors.

## Known limitations

- The 32 public samples cannot measure accuracy on the 518 hidden cases.
- **case_03 / case_44 (analytics_9406 vs analytics_5589)** and **case_27 / case_28 (analytics_4574 vs
  analytics_6077)**: the anonymisations are seconds apart, no evidence carries an identifier and the
  activity order alternates. Fitting the remaining signal would be fitting noise.
- **case_23 (account_410 vs account_7015)**: both accounts are FAILED and both are citable only through
  snapshot omission. Nothing separates them.
- **case_04 (account_004)** appears in no evidence file and no activity row; the only link is a
  naming coincidence, which is not used.
- **case_18 (audit_record_6645) and case_20 (audit_record_5569)** appear in no participant file and cannot be predicted.
- Records that exist only in PDFs or screenshots depend on `multimodal.py`; when it is absent they are
  missed (for example case_07, and the `COMPLETE` rows with empty steps flagged by the validator).
- The classifier fallback is a phrase list. It matches the supplied requests but is not a general
  multilingual intent model; that is what the LLM path is for.

Details of the parsers' findings and integration contract: [`code/parsers/NOTES.md`](./code/parsers/NOTES.md).
Current evaluation numbers: [`code/evaluation/evaluation_report.md`](./code/evaluation/evaluation_report.md).
