# Team-made evaluation cases

These are three hand-built scenarios that do **not** reuse sample cases. Each
directory is a complete mini-dataset with the same files as `dataset/`, plus
an `expected_output.csv`. The expected outputs were written by hand from
`deletion_rules.csv` and the output contract in `problem_statement.md`. They
are not produced by the pipeline.

Rebuild the cases from source:

```bash
python3 code/evaluation/custom_cases/build_cases.py
```

Run them through Person A's wiring:

```bash
python3 tools/run_custom_cases.py      # exit code 1 on any mismatch
```

**Contract for `code/evaluation/main.py` (Person B):** for each directory here,
run the prediction pipeline with this directory as the dataset root. Then
compare its single output row with `expected_output.csv`: the status must match
exactly, and so must the parsed `steps_json` array, in order. These cases need
no request classifier decision beyond "deletion". Their request text is an
unambiguous deletion request.

## (a) `a_temporal_cutoff`: evidence captured after `audit_at` is ignored

| Field | Value |
|---|---|
| Request | `custom_a`, user_901, reported **COMPLETE** on 2026-09-02, audit_at **2026-09-10T12:00Z** |
| evidence_01 (captured 09-05) | account_9001 NOT_FOUND; **search_9001 FOUND**; audit_record_9001 FOUND |
| evidence_02 (captured **09-20**, after the audit) | search_9001 NOT_FOUND |
| Expected | **INCOMPLETE**. REPORT_REMAINING_RECORD search_9001 cites evidence_01, then DELETE_RECORD, then verification of the account (absent), the search entry (absent) and the audit record (present). Remediation and verification steps carry `evidence_id` `""`. |
| What it catches | Using evidence_02 would wrongly make the case COMPLETE. |

The mutation check (run during development) confirms the cutoff decides the
case. Moving audit_at to 09-25 makes evidence_02 usable, and the pipeline
then returns COMPLETE, citing evidence_02 for the search entry.

## (b) `b_recreation_path`: active flow and restorable backup make a "deleted" case INCOMPLETE

| Field | Value |
|---|---|
| Request | `custom_b`, user_902, reported **COMPLETE** |
| evidence_01 (captured 09-05) | account and search entry NOT_FOUND (deleted); export_9002 FOUND; **data_flow_9002 ACTIVE**; **backup_9002 ACTIVE** (restore allowed); audit_record_9002 FOUND |
| Expected | **INCOMPLETE**, with three findings in operation-table order: remaining record, active data flow, restore risk. Remediation is **DISABLE_DATA_FLOW → BLOCK_RESTORE → DELETE_RECORD**, so recreation paths are neutralised before the dependent record is deleted. Verification covers every record and process. |
| What it catches | A case where the primary records are gone but the export could be recreated. |

## (c) `c_retention`: a security audit record is verified present, never deleted

| Field | Value |
|---|---|
| Request | `custom_c`, user_903, reported **COMPLETE** |
| evidence_01 (JSON, captured 09-05) | account_9003 NOT_FOUND; audit_record_9003 FOUND with `retention_until` 2031-12-31; tombstone_9003 REDACTED |
| Activity | A deletion_worker row claims `DELETE_RECORD audit_record_9003 SUCCESS`. This is a workflow claim. |
| Expected | **COMPLETE**, with three cited verification steps: account absent, **audit record present** (rule_12 RETAIN within the retention period), tombstone redacted. There are no DELETE steps. |
| What it catches | Deleting policy-retained records, and trusting a workflow "deleted" claim over evidence. |

## Results

`python3 tools/run_custom_cases.py` passes all three cases.
