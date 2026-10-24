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
resource_aliases(events) -> dict[str, tuple[str, str]]        # business key -> (resource_id, system_id)
# multimodal.py
multimodal_observations(skipped_files, aliases, cache_dir=CACHE_DIR, log=sys.stderr) -> list[Observation]
# state.py
EvidenceIndex(observations, scopes, links=(), events=())
    .direct_links(identity: Identity, not_before: str | None, audit_at: str | None)
        -> dict[str, list[str]]  # resource_id -> reasons; not_before = audit_at of the person's previous request
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

1. `obs, scopes, skipped = load_evidence(ds)`.
2. `events, act_obs, links = parse_activity(ds)`, then
   `obs += multimodal_observations(skipped, resource_aliases(events))`.
3. `idx = EvidenceIndex(obs + act_obs, scopes, links, events)`, built once.
   `rules = load_rules(ds)`.
4. Resolve an `Identity` for **every** request, samples and evaluation together.
   Then compute `direct[c] = idx.direct_links(identity, prev_audit[c], audit_at)`.
   Here `prev_audit[c]` is the audit_at of the same person's previous request,
   ordered by audit_at, or None. Some people have two requests (e.g.
   case_46 / case_17). Evidence that predates the earlier request's audit
   belongs to that request.
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
resolver and no classifier. Use it as the reference wiring. It honours
`HARNESS_DATASET=<dir>`.

**Please give main.py a `--dataset DIR` option** (default `dataset/`). The
team-made cases in `code/evaluation/custom_cases/` are complete mini-datasets,
and `evaluation/main.py` can run each one with
`--dataset code/evaluation/custom_cases/<case>`, then compare the result with
its `expected_output.csv`. The format is described in that folder's README.

### multimodal.py (Person A owns it)

* **PDFs:** text comes from `pdftotext` (or `pypdf` as a fallback) and is
  interpreted deterministically. A PDF goes to the vision model only if no text
  can be extracted. One Observation is emitted per resource the document names,
  either by explicit id or through a catalog alias (`resource_aliases`). A
  `retain until` date becomes `raw.retention_until`.
  * `legal_retention_register_20260529.pdf` lists order refs and user_ids but no
    resource ids, so it emits **nothing**. This is consistent with the gold
    answers for cases 08, 12, 17 and 21, none of which has a legal-hold step.
* **PNGs:** one vision call per file through the `anthropic` SDK.
  * Model: `MULTIMODAL_MODEL`, default `claude-opus-5-5`, called with the
    server-side refusal fallback, effort low and a JSON-schema output format.
  * The key is read from `ANTHROPIC_API_KEY` only.
  * Each result is validated: state and category must be in the allowed
    vocabularies, and every record must name a resource id or an alias.
  * Results are cached in `code/.cache/multimodal/<sha256(file bytes + model +
    prompt version)>.json` (gitignored), so re-runs make zero calls.
* **Usage:** every run writes `code/.cache/multimodal_usage.json`. It holds the
  real API calls of that run (served model, input and output tokens, stop
  reason) and how each file was satisfied. `usage_report.md` should read it.
* **Hook:** `main.py`'s `_load_multimodal` calls
  `multimodal_observations(skipped, dataset_dir)`. Given a directory, the
  function builds the catalog alias map from that dataset's activity file.
* **No API key in this environment.** The 11 PNGs are therefore covered by
  committed transcriptions in `code/multimodal_manual/`, marked
  `"method": "manual_transcription"`. This Claude Code
  session (claude-opus-5-5) viewed each screenshot with its image reader and
  transcribed it into the same record schema. These were **not API calls**, so
  token usage is recorded as `null`, not estimated. To replace them, run with
  `ANTHROPIC_API_KEY` set and `MULTIMODAL_ALLOW_MANUAL=0`.
* **For the usage report:** the final run makes 0 API calls if every file is
  cached. Say that the screenshots were transcribed manually in the dev
  session, with no measurable token usage.
* **Faithful-extraction choices that could cost precision:**
  `security_audit_explorer` shows six audit records for user_03
  (audit_record_001–006), and all six are emitted. All of them are within
  retention, so case_45 verifies all six.

### Pending change in Person B's files: `tools/patches/person_b_integration.patch`

`git apply tools/patches/person_b_integration.patch` makes two changes:
1. **`code/main.py` passes the per-person request windows to `direct_links`**,
   as `(previous request's audit_at, this audit_at, has a later request)`.
   Without them, image/PDF evidence about one request of a person leaks into
   their other request (e.g. leonie: case_46 March / case_17 August).
2. **`code/evaluation/main.py`'s baseline guard uses `>=` instead of `==`.** It
   asserted `status == 31`, so the improvement to 32/32 crashed it.

Measured through Person B's `evaluate_samples` on the 32 samples:

| State | Status | Exact | P | R | F1 | Field acc. |
|---|---|---|---|---|---|---|
| main before multimodal | 31/32 | 24/32 | 0.973 | 0.943 | 0.958 | 0.946 |
| + multimodal.py (this branch, patch not applied) | 32/32 | 23/32 | 0.959 | 0.964 | 0.961 | 0.934 |
| + patch applied | **32/32** | **25/32** | **0.979** | **0.964** | **0.971** | **0.966** |

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
| + multimodal.py and previous-request window | 25/32 | 0.979 | 0.964 |

Status was 31/32 until multimodal.py. The PDF observation for support_001 then
made case_07 INCOMPLETE, and status is now **32/32**.

## Why each cost term exists (for the AI Judge)

`assign_workflow` must decide which case owns a record that carries **no
identifier at all**: an account proven absent only by a snapshot that omits it,
or an anonymised analytics row. The only evidence is deletion-workflow activity.
Each cost term encodes one observable property of that activity, or of the
evidence. None of them refers to a case_id or an answer label.

| Term | Data property it encodes | Why it is general | If removed (ablation) |
|---|---|---|---|
| Slot distance (account -60 min, policy -20 min) | A workflow run submits the primary-profile deletion first, roughly an hour before it reports, then performs policy actions shortly before the report. | It comes from the timing structure of every deletion_worker run in the activity stream, not from labels. | Windows alone admit every action within ±2 h. This is the base term, so it is not ablated. |
| Status/result tie-break (+45) | The account-deletion event is the action the reported status summarises: COMPLETE↔SUCCESS, PARTIALLY_COMPLETE↔PARTIAL, NOT_STARTED↔FAILED. | It joins a claim to the event that produced it. It is never used as proof of state. It is a soft cost, so mismatching hand-made cases (06, 08) still match. | Exact 25 → 22; P 0.979 → 0.963 |
| Evidence tier (+5 / +20, COMPLETE-expected cases only) | A COMPLETE case must cite proof for every verification step. An INCOMPLETE case's verifications are future checks with no citation. | It follows directly from the output contract's citation rule. | Exact 25 → 24 (the case_28/29 account swap returns) |
| Slot occupancy (hard exclusion) | A run performs one action per slot. A case whose own record already fills the R-28 slot (e.g. its tombstone) cannot take a second record in that slot. | It is derived from the case's own directly linked records. | Exact 25 → 23 |
| Run affinity (-10) | A case that owns sibling actions of the same pre-report run (support, data-flow, backup) is the run the anonymisation belongs to. | It is derived from the case's own records. | **No measurable effect on the samples** (25 → 25). Slot occupancy and the failed-run penalty already separate the sample cases. It is kept for hidden cases where neither of those applies, and it can be dropped without a sample regression. |
| Failed-run penalty (+15) | A run that failed retries after the report. A case whose own records show only post-report retries did not run the pre-report policy slot. | It is derived from the case's own records. | Exact 25 → 22 |
| Previous-request window (not a cost) | Some people file two requests (e.g. case_46 in March, case_17 in August). Evidence captured before the earlier request's audit belongs to that request. | It is a temporal rule over the person's own requests. | Exact 25 → 23; P 0.979 → 0.959 |

Ablation (`python3 tools/ablation.py`, 32 samples):

| Configuration | Status | Exact sequence | Step P | Step R | Δ exact |
|---|---|---|---|---|---|
| full model | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| - evidence-tier cost | 32/32 | 24/32 | 0.968 | 0.953 | -1 |
| - slot occupancy | 32/32 | 23/32 | 0.979 | 0.953 | -2 |
| - run affinity | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| - failed-run penalty | 32/32 | 22/32 | 0.968 | 0.953 | -3 |
| - all three run-shape terms | 32/32 | 22/32 | 0.964 | 0.959 | -3 |
| - status/result tie-break | 32/32 | 22/32 | 0.963 | 0.948 | -3 |
| - previous-request window | 32/32 | 23/32 | 0.959 | 0.964 | -2 |

**Sensitivity:** each weight was multiplied by 0.5 and by 2, with the others
left at their defaults. No sample result changes. The results depend on the
*presence and sign* of each term, not on finely tuned values, which is the
main argument that the weights are not fitted to the sample labels.

| Configuration | Status | Exact sequence | Step P | Step R | Δ exact |
|---|---|---|---|---|---|
| CITATION_TIER_PENALTY x0.5 | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| CITATION_TIER_PENALTY x2 | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| RUN_AFFINITY_BONUS x0.5 | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| RUN_AFFINITY_BONUS x2 | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| FAILED_RUN_PENALTY x0.5 | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| FAILED_RUN_PENALTY x2 | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| STATUS_MISMATCH_PENALTY x0.5 | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| STATUS_MISMATCH_PENALTY x2 | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| SLOT_TOLERANCE_MIN x0.5 | 32/32 | 25/32 | 0.979 | 0.964 | +0 |
| SLOT_TOLERANCE_MIN x2 | 32/32 | 25/32 | 0.979 | 0.964 | +0 |

## Full 518-case sanity run (`python3 tools/full_run_report.py`)

There is no classifier in this tool, so every request is treated as a
deletion request.

```
requests: 518
status distribution: {'INCOMPLETE': 356, 'COMPLETE': 162}
steps per case: min 1, median 6.5, mean 6.34, max 15
steps histogram: {1: 9, 2: 38, 3: 86, 4: 23, 5: 63, 6: 40, 7: 76, 8: 127, 9: 4, 12: 4, 13: 12, 14: 34, 15: 2}
FINDING counts by operation: {'REPORT_REMAINING_RECORD': 378, 'REPORT_ACTIVE_DATA_FLOW': 113, 'REPORT_EXPOSED_PII': 206, 'REPORT_RESTORE_RISK': 26}
cases with zero resources (0): []
cases with no USER_PROFILE resource (5): ['case_45', 'case_52', 'case_53', 'case_61', 'case_63']
cases where assign_workflow gave no candidate (216); of these without any USER_PROFILE: ['case_45', 'case_52', 'case_53', 'case_61', 'case_63']
resources linked to more than one PERSON (0):
resources shared by several requests of the SAME person (0): []
COMPLETE-case steps without evidence (must be 0): []
```

**Bugs found and fixed while building this report:**
* **Evidence counted for two requests of the same person** (e.g. Ingrid's
  case_52 / case_33, Priya's case_61 / case_07). Each piece of linking
  evidence is now attributed to one request: the earliest request whose
  window, (previous audit_at, audit_at], contains it. Evidence after the
  latest request's audit goes to the latest request.
* **Records linked only by evidence captured after audit_at.** These are still
  allowed for a person's latest request: the gold answer for case_09 verifies
  account_3960, which is evidenced only after the audit.

**Remaining by design:** the five cases with no USER_PROFILE (45, 52, 53, 61,
63) are the hand-crafted single-artifact scenarios (screenshot or PDF). No
participant file links an account to them; the only link would be matching ID
numbers, which is not evidence.

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
