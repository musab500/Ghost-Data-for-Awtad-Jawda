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
                           direct_links(identity)   resources carrying an identifier
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
| Request classification | LLM (`classifier.py`) with a negation override and a deterministic fallback | Requests are free text in about ten languages and keyword matching misses negation |
| Images (PNG) | Vision model, results cached as JSON (`multimodal.py`, Person A) | Screenshots have no text layer. PDFs are read with `pdftotext`, no model |
| Identity, parsing, temporal state, policy, ordering, validation | Plain Python | Exact matching, timestamps and rule application must be reproducible and auditable |

The model never decides state, policy or step order.

### Modules

| Path | Purpose |
|---|---|
| `code/main.py` | Pipeline, `output.csv` writer, output validator |
| `code/identity.py` | Identity resolution |
| `code/classifier.py` | Deletion classifier, cache, usage log |
| `code/steps.py` | Step assembly and ordering |
| `code/models.py`, `code/parsers/*`, `code/state.py`, `code/rules.py` | Evidence model, parsers, state at `audit_at`, policy engine |
| `code/evaluation/main.py` | Sample evaluation and custom cases (`custom_cases/`) |
| `code/evaluation/classifier_eval.py` | LLM vs fallback vs combined classifier report |
| `code/evaluation/usage_report.py` | Builds `usage_report.md` from recorded API usage |
| `code/tests/` | Unit tests for parsers, state and rules |

## Setup

- Python 3.10 or newer (developed on 3.13). The pipeline uses only the standard library.
- Optional, for the LLM classifier: `pip install anthropic`.
- Optional, for tests: `pip install pytest`.
- `pdftotext` (poppler-utils) is used for PDFs when multimodal extraction runs.

Environment variables (read from the environment or a gitignored `.env` file in
the repository root; they are never printed or logged):

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Enables the LLM classifier (and the vision step). Without it the deterministic fallback is used |
| `GHOST_CLASSIFIER_MODEL` | Override the classifier model (default `claude-haiku-5-5`) |

Example `.env` (never commit it; it is in `.gitignore`):

```
ANTHROPIC_API_KEY=your-key-here
```

## Commands

Run everything from the repository root.

```bash
python3 code/main.py                          # writes ./output.csv (518 rows) and validates it
python3 code/main.py --validate-only output.csv
python3 code/evaluation/main.py               # 32 samples + custom cases, with a regression guard
python3 code/evaluation/classifier_eval.py    # needs a key; fills the classifier section of the report
python3 code/evaluation/usage_report.py       # rewrites evaluation/usage_report.md from recorded usage
PYTHONPATH=code python3 -m pytest code/tests/ -q
```

## Outputs and caching

- `output.csv` (repository root): `case_id,deletion_status,steps_json`, sorted by `case_id`.
- `code/evaluation/evaluation_report.md` and `code/evaluation/usage_report.md`.
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
