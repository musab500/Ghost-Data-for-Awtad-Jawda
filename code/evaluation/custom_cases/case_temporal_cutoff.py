"""Custom case 1: future evidence must be ignored at audit_at.

A profile is FOUND in a snapshot captured before the audit cutoff, then deleted
(NOT_FOUND) in a snapshot captured after it. The audit answer at the cutoff must
reflect the record that was still present — the later deletion cannot be used.
The same resource audited *after* the deletion must instead read NOT_FOUND.
"""
from _util import obs, RULES
from state import EvidenceIndex
from rules import decide

BEFORE = "2026-06-01T00:00:00Z"
AUDIT = "2026-07-01T00:00:00Z"
AFTER = "2026-08-01T00:00:00Z"
LATER_AUDIT = "2026-09-01T00:00:00Z"


def run():
    idx = EvidenceIndex([
        obs("account_900", "account_service", "USER_PROFILE", "FOUND", BEFORE, "ev_before"),
        obs("account_900", "account_service", "USER_PROFILE", "NOT_FOUND", AFTER, "ev_after"),
    ], scopes=[])

    at_cut = idx.state_at("account_900", AUDIT)
    d_cut = decide(at_cut, RULES, AUDIT)
    later = idx.state_at("account_900", LATER_AUDIT)
    d_later = decide(later, RULES, LATER_AUDIT)

    ok = (
        at_cut.state == "FOUND" and at_cut.evidence_id == "ev_before"
        and d_cut.violates and d_cut.finding == "REPORT_REMAINING_RECORD"
        and later.state == "NOT_FOUND" and later.evidence_id == "ev_after"
        and not d_later.violates and d_later.verification == "VERIFY_RECORD_ABSENT"
    )
    detail = (f"@audit state={at_cut.state}({at_cut.evidence_id}) finding={d_cut.finding}; "
              f"@later state={later.state}({later.evidence_id}) verify={d_later.verification}")
    return ("temporal_cutoff", ok, detail)


if __name__ == "__main__":
    print(run())
