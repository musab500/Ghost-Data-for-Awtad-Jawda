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

| Metric | Result |
|---|---|
| Status accuracy | 32 / 32 (1.000) |
| Exact step-sequence match | 25 / 32 (0.781) |
| Step precision | 0.979 (tp=186, fp=4) |
| Step recall | 0.964 (fn=7) |
| Step F1 | 0.971 |
| Field-level accuracy | 0.966 (1305 / 1351 fields) |

## 3. Per-case differences from the gold answers

7 of 32 sample cases differ from the gold sequence; 0 of them also have a wrong status.

| Case | Pred | Gold | Missing | Extra | Documented cause |
|---|---|---|---|---|---|
| case_03 | COMPLETE | COMPLETE | 1 | 1 | analytics_9406 vs analytics_5589: two anonymisations seconds apart, no identifier, inconsistent activity order. |
| case_04 | COMPLETE | COMPLETE | 1 | 0 | account_004 appears in no evidence or activity row; only a naming coincidence links it, which is not used. |
| case_18 | INCOMPLETE | INCOMPLETE | 1 | 0 | audit_record_6645 appears in no participant file. |
| case_20 | INCOMPLETE | INCOMPLETE | 1 | 0 | audit_record_5569 appears in no participant file. |
| case_23 | INCOMPLETE | INCOMPLETE | 1 | 1 | account_410 vs account_7015: both FAILED and citable only by snapshot omission; nothing separates them. |
| case_27 | COMPLETE | COMPLETE | 1 | 1 | analytics_4574 vs analytics_6077: actions 44 s apart with alternating activity order. |
| case_28 | COMPLETE | COMPLETE | 1 | 1 | analytics_4574 vs analytics_6077: mirror of case_27. |

Details of every missing and extra step:

```text
case_03 (pred=COMPLETE, gold=COMPLETE)
  MISSING ('VERIFICATION', 'analytics_platform', 'analytics_9406', 'VERIFY_RECORD_ANONYMIZED', 'evidence_94')
  EXTRA   ('VERIFICATION', 'analytics_platform', 'analytics_5589', 'VERIFY_RECORD_ANONYMIZED', 'evidence_94')
case_04 (pred=COMPLETE, gold=COMPLETE)
  MISSING ('VERIFICATION', 'account_service', 'account_004', 'VERIFY_RECORD_ABSENT', 'evidence_54')
case_18 (pred=INCOMPLETE, gold=INCOMPLETE)
  MISSING ('VERIFICATION', 'security_service', 'audit_record_6645', 'VERIFY_RECORD_PRESENT', '')
case_20 (pred=INCOMPLETE, gold=INCOMPLETE)
  MISSING ('VERIFICATION', 'security_service', 'audit_record_5569', 'VERIFY_RECORD_PRESENT', '')
case_23 (pred=INCOMPLETE, gold=INCOMPLETE)
  MISSING ('VERIFICATION', 'account_service', 'account_410', 'VERIFY_RECORD_ABSENT', '')
  EXTRA   ('VERIFICATION', 'account_service', 'account_7015', 'VERIFY_RECORD_ABSENT', '')
case_27 (pred=COMPLETE, gold=COMPLETE)
  MISSING ('VERIFICATION', 'analytics_platform', 'analytics_4574', 'VERIFY_RECORD_ANONYMIZED', 'evidence_92')
  EXTRA   ('VERIFICATION', 'analytics_platform', 'analytics_6077', 'VERIFY_RECORD_ANONYMIZED', 'evidence_92')
case_28 (pred=COMPLETE, gold=COMPLETE)
  MISSING ('VERIFICATION', 'analytics_platform', 'analytics_6077', 'VERIFY_RECORD_ANONYMIZED', 'evidence_92')
  EXTRA   ('VERIFICATION', 'analytics_platform', 'analytics_4574', 'VERIFY_RECORD_ANONYMIZED', 'evidence_92')
```

## 4. Full-dataset output

The full 518-request run, computed by the same pipeline that writes `output.csv`
(rows are not written by the evaluation script).

| deletion_status | rows | avg steps / row | total steps |
|---|---|---|---|
| COMPLETE | 153 | 3.40 | 520 |
| INCOMPLETE | 355 | 7.75 | 2751 |
| NOT_APPLICABLE | 10 | 0.00 | 0 |
| **total** | 518 |  | 3271 |

COMPLETE rows with no steps: 0

## 5. Multimodal evidence used in this run

| Evidence file | Kind | Extraction method | Observations |
|---|---|---|---|
| evidence_04 | image | manual_transcription | 2 |
| evidence_09 | image | manual_transcription | 2 |
| evidence_14 | image | manual_transcription | 2 |
| evidence_18 | pdf | pdf_text | 1 |
| evidence_19 | image | manual_transcription | 1 |
| evidence_23 | image | manual_transcription | 4 |
| evidence_28 | image | manual_transcription | 1 |
| evidence_32 | image | manual_transcription | 4 |
| evidence_37 | image | manual_transcription | 6 |
| evidence_42 | image | manual_transcription | 1 |
| evidence_46 | image | manual_transcription | 1 |
| evidence_55 | pdf | pdf_text | 1 |
| evidence_58 | pdf | pdf_text | 1 |
| evidence_60 | image | manual_transcription | 1 |

## 6. Request classifier

The classifier's NOT_APPLICABLE decisions are cross-checked against the `CLASSIFY_REQUEST`
workflow flags. Those flags are a workflow claim and are used only as a check, never as the decision.

| Item | Value |
|---|---|
| Classifier mode in this run | deterministic fallback (no model) |
| Classifier calls this run / cache hits | 0 / 0 |
| Classifier NOT_APPLICABLE (full set) | 10 |
| CLASSIFY_REQUEST workflow flags (full set) | 10 |
| Agreement | 10 / 10 |
| Disagreements | none |
| Sample requests classified NOT_APPLICABLE | none |

### LLM versus fallback comparison

Not run. `python3 code/evaluation/classifier_eval.py` needs an `ANTHROPIC_API_KEY`; it writes the LLM-only, fallback-only and combined results to `code/evaluation/classifier_comparison.md`, which this report then includes.

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

| Case | Result | Detail |
|---|---|---|
| guard_compares_counts | PASS | 6 boundary checks |
| non_deletion_classifier | PASS | 6 non-deletion + 3 deletion texts classified correctly |
| retention_and_recreation | PASS | retain.verify=VERIFY_RECORD_PRESENT (violates=False); expired.finding=REPORT_REMAINING_RECORD; flow=REPORT_ACTIVE_DATA_FLOW->DISABLE_DATA_FLOW->VERIFY_DATA_FLOW_DISABLED |
| temporal_cutoff | PASS | @audit state=FOUND(ev_before) finding=REPORT_REMAINING_RECORD; @later state=NOT_FOUND(ev_after) verify=VERIFY_RECORD_ABSENT |
| validator_complete_rule | PASS | strict raised=True; warnings=1 |

5 / 5 passed.

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

0 recorded model call(s), 0 input / 0 output tokens, cost $0.000000; classifier mode in the latest run: fallback

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
