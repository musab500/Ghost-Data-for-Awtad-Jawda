# Evidence parsers — interface and findings (Person A)

## API

```python
from parsers import load_evidence            # sql + json + txt + log
from parsers.activity import parse_activity  # system_activity.csv
observations, scopes, skipped = load_evidence(Path("dataset"))
events, activity_obs, links = parse_activity(Path("dataset"))
```

* `Observation` (models.py) — one resource state seen by one evidence item.
  `observed_at` is when the state was seen, and `captured_at` is the manifest
  time. **Only use or cite an observation when `captured_at <= audit_at`.**
  `raw["_copy"]` holds the SQL table (primary vs replica).
* `Scope` — a complete capture of one system or table. A resource of that
  system that is missing from `present` was **absent** at `observed_at`. This
  is how COMPLETE cases prove `VERIFY_RECORD_ABSENT`: case_01 cites evidence_54,
  and case_31 cites the empty `customer_profiles` snapshot in evidence_51.
* `ResourceLink` — explicit resource↔person link from `LINK_RESOURCE_IDENTITY`.
* `ActivityEvent` — a workflow claim. `reported_result` is never treated as proof.
* `skipped` = 11 PNG + 4 PDF files, which go to multimodal.py.

Identifier tokens are normalised as follows (use the same helpers in identity.py):
`norm_email` lower-cases, `norm_phone` keeps only `+digits`, and user ids are
lower-cased. A hash is `sha256(lower(email))`.

## Findings that matter for identity / state / steps

1. **Historical hashes**: evidence can carry the hash of an *old* email. In
   case_12, analytics_9914 is linked only by the `email_hash` old_value in
   user_audit_log. identity.py must include old emails, the hashes of old
   emails, and the old_values of `email_hash` changes.
2. Log identifiers usually sit on the `*_started` line (lookup_key), while the
   resource and outcome sit on the `*_completed` line. They are joined by trace id.
3. Category corrections compared with the resource-id prefix (both come from
   the samples):
   * legacy contact-index `contact_query` on `support_*` → `SECONDARY_IDENTIFIER`
     → DELETE (case_10)
   * `contact_lookup` with `email_opt_in` on `analytics_*` → `MARKETING_PROFILE`
     → DELETE (case_12)
4. Accounts proven absent by omission, and anonymized analytics rows, contain no
   identifier at all. They link to a case through the `DELETE_RECORD` /
   `ANONYMIZE_RECORD` activity rows in the request window (requested_at to
   reported_at), e.g. account_6440 and analytics_5335 for case_21. Windows can
   overlap, so this needs care.
5. `CLASSIFY_REQUEST` activity rows flag non-deletion requests by account id.
   This is a possible cross-check for the classifier.
6. `RESTORE_BACKUP` and `SYNC_RECORD integration_service→search_service` rows
   record recreation ("repopulated downstream resource X"). See
   `ActivityEvent.related_resources`.
7. Some retained audit records first appear in evidence captured after the
   case's audit_at (e.g. audit_record_4128 for case_23 appears only on 09-17).
   Those cannot be cited. The samples still list them as un-cited
   VERIFY_RECORD_PRESENT steps in INCOMPLETE cases.

Coverage on the 32 sample cases: after SQL, JSON, TXT and log parsing, 36 of
111 expected (system, resource) pairs are not yet linked by a direct
identifier. They fall under items 4 and 7, plus support_001 (PDF, case_07).

## Integration contract (Person A → Person B)

All modules live in `code/`. Run them with `code/` on `sys.path`. Every call is
deterministic, and nothing touches the network.

### Signatures

```python
# parsers/__init__.py
load_evidence(dataset_dir: Path) -> tuple[list[Observation], list[Scope], list[EvidenceFile]]
    # parses sql/json/txt/log; the 3rd value lists the skipped png/pdf files for multimodal.py
# parsers/activity.py
parse_activity(dataset_dir: Path) -> tuple[list[ActivityEvent], list[Observation], list[ResourceLink]]
non_deletion_flags(events) -> dict[str, ActivityEvent]       # account_id -> CLASSIFY_REQUEST row
# state.py
EvidenceIndex(observations, scopes, links=(), events=())
    .direct_links(identity: Identity) -> dict[str, list[str]]  # resource_id -> reasons
    .category_of(resource_id) -> str | None
    .assign_workflow(cases: list[CaseWindow]) -> dict[str, dict[str, str]]  # case_id -> {rid: reason}
    .state_at(resource_id, audit_at) -> ResourceState
    .evidence_tier(resource_id, audit_at) -> int               # 0 explicit, 1 snapshot-only, 2 none
CaseWindow(case_id, reported_at, audit_at, direct_categories: set, reported_status: str,
           expects_complete: bool, direct_resources: set)
flagged_cases(direct_by_case, flags) -> dict[str, str]         # case_id -> activity_id
# rules.py
load_rules(dataset_dir) -> dict[str, Rule]
decide(rs: ResourceState, rules, audit_at) -> Decision | None
    # Decision: system_id, resource_id, rule_id, finding, remediation, verification, evidence_id, violates
OPERATION_ORDER: list[str];  OP_STATE: dict[str, str]
```

`Identity(user_id, emails, phones, email_hashes, names)` uses the normalisers in
models.py. identity.py must include the following (see finding 1 above):
* historical emails and phones from user_audit_log
* `sha256(lower(email))` for every email, current and historical
* the old_value of every `email_hash` change

### Order of calls in main.py

1. `obs, scopes, skipped = load_evidence(ds)`, then
   `obs += multimodal_observations(skipped)`.
2. `events, act_obs, links = parse_activity(ds)`.
3. `idx = EvidenceIndex(obs + act_obs, scopes, links, events)`, built once.
   `rules = load_rules(ds)`.
4. Resolve an `Identity` for **every** request, samples and evaluation together.
   Then compute `direct[c] = idx.direct_links(identity)`.
5. For each case, compute
   `expects_complete = not any(decide(idx.state_at(r, audit), rules, audit).violates for r in direct[c])`.
   Skip None decisions.
6. Call `wf = idx.assign_workflow([...CaseWindow for ALL requests...])` **once**.
   The activity stream is shared across cases, so the assignment is global.
7. For each deletion case (after the classifier), take
   `resources = direct[c] ∪ wf.get(c, {})` and
   `decisions = [decide(idx.state_at(r, audit), rules, audit)]`, dropping None.
8. Build the steps:
   * Status is INCOMPLETE if any decision violates, otherwise COMPLETE.
   * Phase order is FINDING, then REMEDIATION, then VERIFICATION. Within a phase,
     sort by `(OPERATION_ORDER.index(op), system_id, resource_id)`. Each step's
     `state` is `OP_STATE[op]`.
   * FINDING steps cite `decision.evidence_id`.
   * REMEDIATION and VERIFICATION steps in INCOMPLETE cases use `""`.
   * COMPLETE cases contain verification steps only, each citing its
     evidence_id. Drop any decision whose evidence_id is empty.
   * NOT_APPLICABLE cases use `[]`.

`tools/sample_harness.py` implements steps 1–8 with a stand-in identity
resolver and no classifier. Use it as the reference wiring.

### What multimodal.py must emit

Emit the same `Observation` type. Set `source_kind` to "pdf" or "image",
`evidence_id` to the file_id, and `captured_at` to the manifest time. Put every
identifier that appears in the file (emails, phones, user_ids) in
`identifiers`, so that `direct_links` finds the record.

* **PDFs are text, so no OCR or LLM is needed.** `pdftotext -layout` is
  available in this environment and extracts them cleanly.
* `documents/delivery_correction_attachment.pdf` (evidence_58) contains
  `support_001`, `priya.raman@gmail.com`, a phone number and a file name.
  Emit:
  `Observation("support_001", "support_platform", "SUPPORT_ATTACHMENT", "FOUND", {priya.raman@gmail.com, +919876573142}, ..., "evidence_58", "pdf")`.
  This makes case_07 INCOMPLETE: REPORT_REMAINING_RECORD, DELETE_RECORD,
  VERIFY_RECORD_ABSENT, citing evidence_58.
* `documents/security_retention_extract.pdf` (evidence_55) contains
  `audit_record_003`, subject `user_03`, and "retain until 25 Aug 2027". Emit
  SECURITY_AUDIT_LOG FOUND with `raw={"retention_until": "2027-08-25T04:03:00Z"}`.
* `documents/legal_retention_register_20260529.pdf` names user_08, user_12,
  user_17, user_21, user_24 and user_29, which suggests LEGAL_HOLD_RECORD rows.
  `documents/account_history_report_20260718.pdf` names user_18.
* **Correction to the earlier brief:** `images/security_audit_explorer_20260905.png`
  shows **user_03's** events (audit_record_001–006), *not* audit_record_6645 or
  audit_record_5569. Those two IDs occur in **no** participant file: no text,
  PDF, activity row or the two images I viewed. See the known limitations below.
* The images I viewed each describe a single hand-crafted user.
  `account_admin_console_20260926.png` shows user_11, account_5762 and
  support_9538. So the 11 PNGs mainly matter for the low-numbered "special"
  users. No tesseract is installed, so the PNGs need a vision model, with
  results cached as JSON.

### Classifier cross-check (CLASSIFY_REQUEST)

`non_deletion_flags(events)` returns the 10 accounts that the privacy-intake
workflow classified as non-deletion. `flagged_cases(direct, flags)` maps them to
cases through direct links. Today they map one-to-one onto case_533 to case_542.
Every one of those request texts is a non-deletion request: corrections, data
copies, marketing opt-outs, login help.

Use these flags only to **cross-check** the LLM classifier, and report the
agreement in the evaluation report. They are a workflow claim, so do not let
them override the classifier.

## Workflow assignment (state.assign_workflow)

Some resources carry no identifier at all: accounts proven absent by omission,
and anonymised analytics rows. These are linked through deletion_worker
activity, with a global minimum-cost one-to-one matching per category
(Hungarian algorithm).

**Candidate windows** (minutes relative to reported_at):
* primary-profile deletion: [-90, +10]
* other policy actions: [-35, +10]

**Cost terms:**
* Distance from the expected slot: -60 for the account deletion, -20 for
  policy actions.
* +45 when the account deletion's reported_result disagrees with the case's
  reported_status. This is a tie-breaker only.
* For a case expected to be COMPLETE: +5 if the candidate is citable only
  through snapshot omission, +20 if it is not citable at all. An INCOMPLETE
  case's verifications need no citation, so it pays nothing.
* Workflow-run shape, for non-account actions only:
  * A case that already owns a resource acted on within 2 minutes of the
    candidate's slot cannot take it.
  * -10 if the case owns sibling actions in the same pre-report run.
  * +15 if the case's own actions are only post-report retries.

**Sample harness progression:**

| Stage | Exact sequence | Step P | Step R |
|---|---|---|---|
| Baseline | 20/32 | 0.947 | 0.927 |
| + evidence-tier cost | 21/32 | 0.958 | 0.938 |
| + run-shape terms | 24/32 | 0.973 | 0.943 |

Status stays at 31/32 throughout. The only status miss is case_07, which is
waiting on the PDF observation.

## Known limitations (documented, deliberately not fitted)

* **case_03 / case_44: analytics_9406 vs analytics_5589.** Both cases have the
  same reported_at and both runs need one R-28 analytics slot. The two
  anonymisations are 15 s apart. No evidence carries an identifier, and
  activity order is not consistent across runs.
* **case_27 / case_28: analytics_4574 vs analytics_6077.** This is the same
  situation, with the actions 44 s apart. Within this block, activity order
  alternates between the two cases (analytics 27,28; support 28,27; flow 27,28;
  backup 28,27). The only remaining signal would be the generator's per-second
  jitter, and fitting that would be fitting noise.
* **case_23: account_410 vs account_7015.** Several NOT_STARTED cases share
  reported_at 09-05T12:00. Both accounts are FAILED and both are citable only
  through snapshot omission. Nothing in the data separates them.
* **case_04: account_004.** This account appears in no evidence file and no
  activity row. The gold answer cites evidence_54 (absence by omission). The
  only "link" is the matching number (user_04 / account_004). That is a naming
  coincidence, not evidence, so it is not used.
* **case_18 audit_record_6645 and case_20 audit_record_5569.** These IDs appear
  in no participant file, so they cannot be predicted.
* **Accuracy on the hidden set.** The 32 samples cannot show this. The
  assignment costs were checked for generality, not tuned per label, but the
  sample set is small.
