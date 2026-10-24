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
