# Evaluation Report — Ghost Data Still Exists

<!-- GENERATED FILE. Edit code/evaluation/report_template.md for narrative; every number below is
     written by `python3 code/evaluation/main.py` from the current run. -->

This report is produced by `python3 code/evaluation/main.py`. The narrative comes from
`code/evaluation/report_template.md`; every number, table and per-case result is computed by the
script from the current code and evidence. Re-running the script refreshes all of them. The script
also fails if the sample results regress: fewer correct steps, more extra or missing steps, or lower
status or sequence accuracy than the baseline defined in `code/evaluation/main.py`.

## 1. Architecture under test

Deterministic core; a model is used only where it adds value.

| Stage | Module | Model? |
|---|---|---|
| Identity resolution (current and historical ids, email hashes) | `identity.py` | no |
| Evidence parsing (sql/json/txt/log) and activity | `parsers/*` | no |
| Multimodal evidence (PDF/PNG to observations) | `multimodal.py` | PNG: vision model; PDF: text extraction |
| State reconstruction at `audit_at` | `state.py` | no |
| Policy decision per resource | `rules.py` | no |
| Per-person request windows (which resources belong to which request) | `state.link_requests` | no |
| Workflow assignment (identifier-less resources) | `state.assign_workflow` | no |
| Request classification | `classifier.py` via `llm_provider.py` | LLM (Gemini or Anthropic) when a key is set; deterministic fallback otherwise |
| Step assembly and ordering | `steps.py` | no |
| Output and structural validation | `main.py` | no |

## 2. Sample evaluation (public samples vs `dataset/sample_output.csv`)

Step identity is the tuple `(phase, system_id, resource_id, operation, evidence_id)`. Field-level
accuracy aligns predicted and gold steps by position and compares all seven step fields; a length
difference counts the missing fields as wrong.

{{sample_metrics}}

## 3. Per-case differences from the gold answers

{{diff_summary}}

Details of every missing and extra step:

{{diff_details}}

## 4. Full-dataset output

The full 518-request run, computed by the same pipeline that writes `output.csv`
(rows are not written by the evaluation script).

{{full_run}}

## 5. Multimodal evidence used in this run

{{multimodal}}

## 6. Request classifier

The classifier's NOT_APPLICABLE decisions are cross-checked against the `CLASSIFY_REQUEST`
workflow flags. Those flags are a workflow claim and are used only as a check, never as the decision.

{{classifier_crosscheck}}

### LLM versus fallback comparison

{{classifier_comparison}}

## 7. Team-authored evaluation cases

Each case under `code/evaluation/custom_cases/` builds a small in-memory evidence set and checks one
behaviour the public samples cannot isolate.

- `temporal_cutoff`: a profile present before `audit_at` and deleted after it must be reported as
  present at the cutoff (future evidence ignored), and as absent when audited later.
- `non_deletion_classifier`: access, copy, correction, marketing opt-out, login-help and keep-account
  requests are NOT_APPLICABLE; genuine erasure requests, including one that says "remove any
  remaining copy", stay deletions.
- `retention_and_recreation`: an audit log inside its retention window is kept, the same record past
  retention becomes a deletion finding, and an active data flow must be disabled and verified.
- `validator_complete_rule` (regression): an empty COMPLETE row must make strict validation raise and
  must produce one warning in non-strict mode.

Results from this run:

{{custom_cases}}

## 8. Failures found and improvements made

- **Classifier false positive.** The phrase "copy of my" matched "remove any remaining copy of my
  profile", a genuine deletion. The signal was narrowed to specific access phrases, and the case is
  covered by the `non_deletion_classifier` check.
- **Empty COMPLETE rows.** When evidence exists only in images or PDFs that were not loaded, the
  pipeline emitted COMPLETE with no steps. The validator now rejects a COMPLETE row without a
  VERIFICATION step or with an uncited step; it reports warnings only while no multimodal
  observation is loaded.
- **Stale reports.** Hand-typed numbers drifted from the code. Numbers are now written by the
  script from the current run, and the pass thresholds live only in code.
- **Silent classifier fallback.** LLM failures used to be swallowed. They are now printed, and a
  notice is printed whenever the classifier runs in fallback mode.
- **Loud output validation.** `main.validate_output_file` re-checks every row: row count, sort order,
  JSON validity, consecutive `step_no`, phase and operation legality, ordering, and the evidence
  citation contract.

## 9. Usage and cost (summary)

{{usage_summary}}

Full breakdown: `usage_report.md`.

## 10. Limitations

- The public samples are a small set. They confirm the status logic and the step contract but
  cannot measure accuracy on the hidden cases. Workflow assignment costs were checked for
  generality, not tuned to individual labels.
- Near-simultaneous records that carry no identifier remain ambiguous by construction: the status
  stays correct and only the cited resource id can differ.
- Records evidenced only in images or PDFs are available only when the multimodal observations
  are loaded; section 5 lists what this run used.
- The fallback classifier is a phrase list; it matches the supplied requests but is not a general
  multilingual intent model.
