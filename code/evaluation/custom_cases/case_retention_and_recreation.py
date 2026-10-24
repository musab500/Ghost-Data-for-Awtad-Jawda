"""Custom case 3: retention policy and recreation-path handling.

Three checks the samples cannot isolate cleanly:
  a) a SECURITY_AUDIT_LOG within its retention window must be kept
     (VERIFY_RECORD_PRESENT, no finding);
  b) the same record past its retention window flips to DELETE (a finding);
  c) an ACTIVE data flow is a recreation risk that must be disabled
     (REPORT_ACTIVE_DATA_FLOW -> DISABLE_DATA_FLOW -> VERIFY_DATA_FLOW_DISABLED).
"""
from _util import obs, RULES
from state import EvidenceIndex
from rules import decide

AUDIT = "2026-07-01T00:00:00Z"
SEEN = "2026-06-15T00:00:00Z"


def run():
    idx = EvidenceIndex([
        obs("audit_record_900", "security_service", "SECURITY_AUDIT_LOG", "FOUND",
            SEEN, "ev_sec_hold", raw={"retention_until": "2027-08-25T00:00:00Z"}),
        obs("audit_record_901", "security_service", "SECURITY_AUDIT_LOG", "FOUND",
            SEEN, "ev_sec_exp", raw={"retention_until": "2026-01-01T00:00:00Z"}),
        obs("data_flow_900", "integration_service", "DATA_FLOW", "ACTIVE",
            SEEN, "ev_flow"),
    ], scopes=[])

    retain = decide(idx.state_at("audit_record_900", AUDIT), RULES, AUDIT)
    expired = decide(idx.state_at("audit_record_901", AUDIT), RULES, AUDIT)
    flow = decide(idx.state_at("data_flow_900", AUDIT), RULES, AUDIT)

    ok = (
        retain is not None and not retain.violates
        and retain.verification == "VERIFY_RECORD_PRESENT"
        and expired is not None and expired.violates
        and expired.finding == "REPORT_REMAINING_RECORD"
        and flow is not None and flow.violates
        and flow.finding == "REPORT_ACTIVE_DATA_FLOW"
        and flow.remediation == "DISABLE_DATA_FLOW"
        and flow.verification == "VERIFY_DATA_FLOW_DISABLED"
    )
    detail = (f"retain.verify={retain.verification} (violates={retain.violates}); "
              f"expired.finding={expired.finding}; "
              f"flow={flow.finding}->{flow.remediation}->{flow.verification}")
    return ("retention_and_recreation", ok, detail)


if __name__ == "__main__":
    print(run())
