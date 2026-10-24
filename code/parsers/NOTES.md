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

## state.py / rules.py (Person A) — integration recipe for main.py

```python
from state import EvidenceIndex, CaseWindow
from rules import load_rules, decide, OPERATION_ORDER, OP_STATE

obs, scopes, skipped = load_evidence(ds)           # + multimodal observations
events, act_obs, links = parse_activity(ds)
idx = EvidenceIndex(obs + act_obs, scopes, links, events)   # build once
rules = load_rules(ds)

direct = {c: idx.direct_links(identity_of[c]) for c in all_cases}     # samples + eval
wf = idx.assign_workflow([CaseWindow(c, req.reported_at, req.audit_at,
                          {idx.category_of(r) for r in direct[c]}, req.reported_status)
                          for c in all_cases])     # global, run once over ALL requests
for c in deletion_cases:
    for rid in direct[c] | wf.get(c, {}):
        d = decide(idx.state_at(rid, req.audit_at), rules, req.audit_at)   # Decision or None
```

Step assembly (as validated against the samples):
* The status is INCOMPLETE if any `d.finding` exists, otherwise COMPLETE.
* Phases go FINDING, then REMEDIATION, then VERIFICATION. Within a phase, sort by
  `(OPERATION_ORDER.index(op), system_id, resource_id)`. Each step's `state` is `OP_STATE[op]`.
* Evidence: FINDING steps cite `d.evidence_id`. REMEDIATION and VERIFICATION
  steps in INCOMPLETE cases use `""`.
* COMPLETE cases contain verification steps only, each citing `d.evidence_id`.
  Drop any decision whose `evidence_id` is empty (it cannot be cited).

Scratch end-to-end check on the 32 samples, using a stand-in identity resolver,
no classifier, and no image/PDF evidence:
* status 31/32
* exact step sequence 20/32
* step precision 0.947, recall 0.927

The remaining misses break down as:
* records only present in PNG/PDF files: support_001, audit_record_6645 and
  audit_record_5569
* identifier-less accounts or analytics records shared by cases with the
  *same* reported_at, which the evidence cannot tell apart

Full 518-case run: no crashes, and every request email resolved to a user.
Cases 45, 61 and 63 currently get no resources. Their resources are linked
only through catalog links or image/PDF evidence (audit_record_003,
object_19, marketing_001).
