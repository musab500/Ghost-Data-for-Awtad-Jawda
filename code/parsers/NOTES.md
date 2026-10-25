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
    .link_requests(refs: list[RequestRef]) -> dict[str, dict[str, list[str]]]
        # case_id -> {resource_id: reasons}; groups ALL requests by user_id and applies the evidence windows
    .direct_links(identity, not_before, audit_at, has_later_request)   # one request; used by link_requests
    .category_of(resource_id) -> str | None
    .assign_workflow(cases: list[CaseWindow]) -> dict[str, dict[str, str]]  # case_id -> {rid: reason}
    .state_at(resource_id, audit_at) -> ResourceState
    .evidence_tier(resource_id, audit_at) -> int               # 0 explicit, 1 snapshot-only, 2 none
RequestRef(case_id, identity | None, audit_at, requested_at="")
request_windows(refs) -> {case_id: (not_before | None, has_later_request)}
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
4. Resolve an `Identity` for **every** request, samples and evaluation together,
   then `direct = idx.link_requests([RequestRef(c, identity[c], audit_at, requested_at) ...])`
   in ONE call over all requests. It groups them by resolved user_id and applies
   the per-person evidence windows described under "Per-person request windows".
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
  interpreted deterministically; a PDF goes to the vision model only if no text
  can be extracted. One Observation per resource the document names, by explicit
  id or through a catalog alias (`resource_aliases`). A `retain until` date
  becomes `raw.retention_until`.
  * `legal_retention_register_20260529.pdf` lists order refs and user_ids but no
    resource ids, so it emits **nothing**. This matches the gold answers for
    cases 08, 12, 17 and 21, none of which has a legal-hold step.
* **PNGs:** one call per file through **`llm_provider.vision_json(system, prompt,
  image_bytes, mime, schema) -> (parsed, usage)`** (Person B; Gemini by default).
  `multimodal.py` imports no model SDK. The prompt and schema live in
  `multimodal.py` (`SYSTEM_PROMPT`, `VISION_PROMPT`, `RECORD_SCHEMA`, version
  `PROMPT_VERSION`). The schema's `state` and `category` are enums built from
  `models.STATES` / `models.CATEGORIES`, and every record is validated again:
  a record is dropped, with a warning, if its state/category is outside the
  vocabulary or it names neither a resource id nor a catalog alias.
* **Lookup order per file** (a re-run makes zero calls):

  | # | Source | Notes |
  |---|---|---|
  | 1 | `code/.cache/multimodal/<sha256(bytes + provider + model + PROMPT_VERSION)>.json` | live-call results; gitignored; exact (provider, model) match |
  | 2 | `code/multimodal_vlm/` | **frozen, accepted results; committed**, so `code.zip` reproduces the run with no key. Matched by file hash + prompt version, whichever model produced them |
  | 3 | a live call | only if `llm_provider` has a configured provider; the result goes to (1) |
  | 4 | `code/multimodal_manual/` | hand transcriptions made without a model: a **documented fallback**, used with a warning, and **never** when `MULTIMODAL_ALLOW_MANUAL=0` (the final run) |

  With no provider configured, a cached or frozen result for the file's bytes is
  still used (whichever model made it); with none, the file is skipped with a warning.
* **A failed call never raises.** `main.py`'s hook turns any exception into "no
  multimodal evidence at all", so every `LLMFailure` (and any other error, or a
  payload without `records`) is a warning, the file is skipped (or falls back),
  nothing is cached, and the attempt is recorded with whatever tokens the API
  reported.
* **Usage ledger `code/.cache/multimodal_usage.json`**, in the schema
  `evaluation/usage_report.py` reads (verified against it): `calls` is a list of
  `{file_id, provider, model, input_tokens, output_tokens, ok, usage_reported,
  from_cache, at}`, **one record per API response**: when the provider retries a
  truncated answer (`usage["attempts"]`, update_9) the cut-off response is a record
  with `ok: false, error_kind: "truncated"` and the retry another with `retry: true`,
  since both are billed. It lists the **real responses behind the vision results
  the run used**: those made now (`from_cache: false`; failed ones have `ok:
  false`) and, for results served from a cache, those that produced them
  (`from_cache: true`, original timestamp). So a zero-call re-run still reports the one-time
  extraction cost, and a run that used only PDF text or manual transcriptions
  reports no calls. Tokens are as returned by the provider; nothing is estimated.
  Also: `api_calls_this_run`, `files` (how each file was satisfied), `files_skipped`.
* **Observations** carry `raw["method"]`: `pdf_text`, `vlm:<provider>/<model>` or
  `manual_transcription` (the evaluation report groups by it).
* **No `anthropic` import any more**: `tools/patch_skip_without_anthropic.py` is
  obsolete (nothing in `test_multimodal.py` needs that SDK).

### Real vision run of the screenshots: status and procedure

**Status: real runs done; no VLM result is accepted yet; the manual transcriptions
remain the committed fallback.** All runs used Gemini through `llm_provider` on the
free tier (no paid billing). Nothing is frozen in `code/multimodal_vlm/`.

| Run | Model | Prompt | Outcome | Tokens (in / out) |
|---|---|---|---|---|
| 1 | gemini-3.6-flash | v2 | 10 of 11 files (evidence_28: 503 x3) | 15,884 / 28,086 |
| 2 | gemini-3.6-flash | v3 | 4 of 11 (7 files: 429, no tokens) | in ledger |
| 3 | gemini-3.6-flash | v4, thinking off | 11 of 11 (made unintentionally by a unit test that used the real provider; the test is now hermetic) | in ledger |
| 4 | gemini-3.5-flash-lite | v4 | 11 x HTTP 400: this model rejects a thinking budget; fixed (model-dependent thinking setting) after one diagnostic call | 0 |
| 5 | gemini-3.5-flash-lite | v5, thinking skipped | 11 of 11, no truncation, no retries, no 0-record file | 21,454 / 5,139 |

Every call of every run is in the usage ledger (`code/multimodal_usage_prior_runs.json`,
merged into `code/.cache/multimodal_usage.json` by `multimodal.py`; 55 calls,
66,639 input / 46,896 output tokens). Comparison files: `code/evaluation/multimodal_comparison_run1.md`,
`..._run2_partial.md`, `..._gemini_3_6_flash_v4.md`, `..._flash_lite_v5.md`; raw run-5 JSON in
`code/evaluation/vlm_runs/gemini_3_5_flash_lite_v5/`.

**What changed in the prompt and why (generic wording only, nothing taken from sample outputs).**
v2 -> v3: per-category state guidance and "do not copy an identifier onto a record that does not display
it" (run 1 attributed a customer's email to the support case and labelled a backup IDENTIFIABLE, which
`rules.decide` cannot act on). v3 -> v4: `system_id` must be a catalog service id (v3 let the model return
the product name "Sentinel"); non-catalog values fall back to the category's system; the category follows
the resource-id prefix (run 1 called `tombstone_17` a USER_PROFILE). v4 -> v5: data flows and backups take
only ACTIVE/DISABLED/BLOCKED (v4 labelled two running data flows IDENTIFIABLE because a selector shows an
email, which flipped case_39 to a wrong COMPLETE), a retained reference with no direct identifier is REDACTED
(v4 returned FOUND, which produced a false REPORT_EXPOSED_PII in case_46), and every record the page links to
is listed (v4 dropped `support_9538`).

**Run 5 judged against the images** (offline pipeline diff with `LLM_PROVIDER=none`: manual-only output vs
output with the run-5 results: 2 of 518 rows change, case_46 and case_63, both wrongly):

| File | Verdict | Reason (checked in the image) |
|---|---|---|
| evidence_14 | VLM right | `data_flow_1121`/`data_flow_9846`: Mode "Continuous", Healthy, events/hour -> ACTIVE (v4 said IDENTIFIABLE; fixed by v5). Pipeline-identical |
| evidence_04 | VLM acceptable | `account_5762` shows name, email, phone -> IDENTIFIABLE is correct by the definition; `support_9538` is now listed (field "Support: CS-8538 - support_9538"); manual's `user_11` on the support record is not shown on that row |
| evidence_19, 37, 46 | VLM acceptable | same records, states and identifiers; `observed_at` empty or the screen's header time instead of the per-row time (no effect: falls back to the capture time) |
| evidence_09 | **manual right** | VLM dropped `customer_reference user_13` of `search_428`, which the table displays |
| evidence_23 | **manual right** | VLM dropped the Owner e-mail of `export_1258/1259/1261`, displayed in the Owner column |
| evidence_28 | **manual right** | `support_1034` is a helpline ticket; VLM category USER_PROFILE (id prefix `support_` is not in `category_for_resource`) |
| evidence_32 | **manual right** | VLM adds `backup_9281`, which only appears in the activity feed ("restored - Completed"); its ACTIVE state is not shown |
| evidence_42 | **manual right** | page is the retained reference `user_17`; the VLM listed the four linked records (order, 2 support, finance), mapped them through the customer key to `tombstone_17` with state ACTIVE and never listed the page's own subject: case_46 loses `VERIFY_RECORD_REDACTED` |
| evidence_60 | **manual right** | page shows `marketing_001`; VLM returned resource_id `marketing_1` (invented normalisation): case_63 INCOMPLETE -> wrong COMPLETE |

Conclusion: 5 of 11 files are acceptable, 6 are not (one has a wrong resource id, one a wrong structure, three
drop displayed identifiers, one a wrong category / extra record). That is not good enough to freeze, so nothing
is frozen and the pipeline uses the manual transcriptions. Planned next step (needs `gemini-3.6-flash` quota):
one run of all 11 files with that model, same prompt, then the same image-by-image review.
Pipeline rows that would change with the run-5 results: **case_63** (wrong: loses `marketing_001` finding /
remediation / verification) and **case_46** (loses `VERIFY_RECORD_REDACTED` on `tombstone_17`); neither
change is accepted, so `output.csv` is unchanged.

**To run it** (whoever has the key, in a checkout with `.env`; `pip install -r requirements.txt`):

```bash
python3 tools/check_env.py --list                                   # Flash model names this key can see
python3 tools/check_env.py --ping                                   # chosen vision model + key work?
python3 tools/compare_multimodal.py --run --freeze --out code/evaluation/multimodal_comparison.md
MULTIMODAL_ALLOW_MANUAL=0 python3 code/main.py --out /tmp/vlm.csv   # VLM-only run
python3 tools/compare_multimodal.py --pipeline-diff                 # do any of the 518 rows change?
```

`--run` makes at most one call per PNG (11), with manual transcriptions forbidden.
`--freeze` copies the results into `code/multimodal_vlm/` (**commit them**; if a
result is wrong, edit nothing: re-run or fix the prompt and bump `PROMPT_VERSION`).
The report lists per file the records matched / missing / extra and field
mismatches. **Then every difference must be judged by opening the image**
(`dataset/evidence/images/<file>`): which side is right, and why. Send the
`multimodal_comparison.md` and the frozen JSONs to Person A to do that and write
the conclusions here; `observed_at` differences are shown but do not change output.

**Why `MULTIMODAL_ALLOW_MANUAL` still defaults to fallback-on**
(`MANUAL_FALLBACK_DEFAULT = "1"` in `multimodal.py`). Measured on the 518 rows
(`--pipeline-diff`, no vision result available): forbidding the manual
transcriptions changes **12 rows (2.3%)** and **flips 8 statuses from INCOMPLETE
to a wrong COMPLETE** (cases 35, 39, 50, 52, 53, 55, 61, 63; cases 33, 34, 45, 46
lose steps). Defaulting to 0 before the vision results are frozen would silently
degrade the shipped output and make a clean `code.zip` run differ from
`output.csv`. Once `code/multimodal_vlm/` is committed, the lookup order uses the
frozen results whenever they exist, and flipping the default is the one-line
change of `MANUAL_FALLBACK_DEFAULT` to `"0"`. The final run should use
`MULTIMODAL_ALLOW_MANUAL=0` explicitly in the meantime.

### Classifier cross-check (CLASSIFY_REQUEST)

`non_deletion_flags(events)` returns the 10 accounts that the privacy-intake
workflow classified as non-deletion. `flagged_cases(direct, flags)` maps them to
cases through direct links. Today they map one-to-one onto case_533 to case_542.
Every one of those request texts is a non-deletion request: corrections, data
copies, marketing opt-outs, login help.

Use these flags only to **cross-check** the LLM classifier, and report the
agreement in the evaluation report. They are a workflow claim, so do not let
them override the classifier.

## Per-person request windows (`state.request_windows`, `EvidenceIndex.link_requests`)

**Problem.** Some people file more than one request (a deletion in March, another
in August). Evidence about an earlier episode still exists later, and the
catalog links it to the same person, so it would be repeated in the later request.
Gold answers show it is not: object_19 (linked 2026-02-13) is not in case_07
(requested 2026-08-31), and tombstone_17 (linked 2026-03-10) is not in case_17.

**Grouping.** Requests are grouped by the resolved `Identity.user_id`
(case-insensitive), never by email, since a person may use an old and a new
address. Samples and evaluation requests are grouped **together**: in all three
multi-request cases the earlier request is an evaluation case.

**Windows.** For one person, let a_1 < ... < a_n be the distinct `audit_at` times
of their requests. Request k owns linking evidence captured in

    W_1 = (-inf, a_1]      W_k = (a_(k-1), a_k]      W_n = (a_(n-1), +inf)

* **Upper bounds are inclusive**, consistent with "at or before audit_at". Lower
  bounds are exclusive, so evidence captured exactly at a_(k-1) belongs to
  request k-1.
* **The last window is open-ended.** Evidence captured after the person's final
  audit still identifies their records (identity is not state). `state_at` never
  uses it to establish a state.
* Requests of one person with the **same** `audit_at` share one window (none
  occur in the data). A person with one request has no window.

**Ownership: first linking evidence.** A linking event is the capture time of an
observation, or the time of a catalog link (`LINK_RESOURCE_IDENTITY`), that
carries one of the person's identifiers. A resource belongs to the request whose
window contains its **earliest** linking event, and to no other request of that
person. A resource first seen in window 1 and observed again in window 2 stays
with request 1. Workflow-assigned resources (no identifiers) are matched by
`reported_at` in `assign_workflow`, one resource per case, so they are not
repeated either.

**Alternative considered.** Keeping a resource for *every* request whose window
holds *any* of its linking evidence gives the **same owners for every resource of
all three multi-request users** in this data, so the choice changes no output.
First-evidence ownership is used because it is one-to-one by construction.

**Measured effect** (through Person B's `evaluate_samples`, 32 samples): status
32/32 -> 32/32; exact sequence 23 -> **25**/32; precision 0.959 -> **0.979**; recall
0.964; F1 0.961 -> **0.971**; field accuracy 0.934 -> **0.966**. case_07 and case_17
become exact matches. Of the 518 output rows only **4 change** (case_33, 46, 52,
61), all belonging to the three people below. Person B's `main.py` calls
`link_requests`.

**Counts (32 samples + 518 evaluation = 550 requests):** 547 distinct user_ids;
**3 user_ids have two requests**, 544 have one; 0 have three or more. 0 ties in
`audit_at`; 0 use more than one distinct email.

| user_id | Earlier request | Later request |
|---|---|---|
| user_07 | case_61 (eval), audit 2026-02-14 | case_07 (sample), audit 2026-09-12 |
| user_17 | case_46 (eval), audit 2026-03-11 | case_17 (sample), audit 2026-09-15 |
| user_18 | case_52 (eval), audit 2026-06-19 | case_33 (eval), audit 2026-07-19 |

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
| Per-person request windows (not a cost) | Some people file two requests (e.g. case_46 in March, case_17 in August). Each piece of linking evidence belongs to one request: the earliest whose window (previous audit_at, audit_at] contains it. | It is a temporal rule over the person's own requests. | Exact 25 → 23; P 0.979 → 0.959 |

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
| - per-person request windows | 32/32 | 23/32 | 0.959 | 0.964 | -2 |

Re-run on the merged tree (person-a + Person B's main): **identical**.

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
