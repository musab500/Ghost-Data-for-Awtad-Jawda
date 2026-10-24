"""Synthetic tests for state.py and rules.py (Person A). These do not reuse
sample cases; each builds a tiny evidence world by hand.
Run from repo root:  python3 -m unittest discover -s code/tests -v
"""
import sys
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
DATASET = CODE.parent / "dataset"

from models import ActivityEvent, Identity, Observation, Scope  # noqa: E402
from rules import decide, load_rules  # noqa: E402
from state import CaseWindow, EvidenceIndex  # noqa: E402

RULES = load_rules(DATASET)
AUDIT = "2026-09-15T12:00:00Z"


def ob(rid, system, cat, state, t, ev, ids=(), captured=None, raw=None):
    return Observation(rid, system, cat, state, set(ids), t, ev, "test",
                       raw=raw or {}, captured_at=captured or t)


def ops(d):
    return (d.finding, d.remediation, d.verification) if d else None


class Temporal(unittest.TestCase):
    def test_future_evidence_ignored(self):
        # Search entry FOUND before audit; a NOT_FOUND captured after audit_at
        # must not make the case look compliant.
        idx = EvidenceIndex([
            ob("search_1", "search_service", "SEARCH_INDEX_ENTRY", "FOUND",
               "2026-09-10T00:00:00Z", "evidence_a", ["a@x.com"]),
            ob("search_1", "search_service", "SEARCH_INDEX_ENTRY", "NOT_FOUND",
               "2026-09-20T00:00:00Z", "evidence_b", ["a@x.com"]),
        ], [])
        rs = idx.state_at("search_1", AUDIT)
        self.assertEqual((rs.state, rs.evidence_id), ("FOUND", "evidence_a"))
        self.assertEqual(ops(decide(rs, RULES, AUDIT)),
                         ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"))

    def test_file_captured_after_audit_is_unusable_even_if_row_older(self):
        idx = EvidenceIndex([
            ob("search_1", "search_service", "SEARCH_INDEX_ENTRY", "NOT_FOUND",
               "2026-09-10T00:00:00Z", "evidence_late", captured="2026-09-16T00:00:00Z"),
        ], [])
        self.assertIsNone(idx.state_at("search_1", AUDIT).state)

    def test_latest_observation_wins_and_any_replica_copy_counts(self):
        t = "2026-09-14T00:00:00Z"
        idx = EvidenceIndex([
            ob("account_1", "account_service", "USER_PROFILE", "NOT_FOUND", "2026-09-01T00:00:00Z", "e1"),
            ob("account_1", "account_service", "USER_PROFILE", "NOT_FOUND", t, "e2"),   # primary
            ob("account_1", "account_service", "USER_PROFILE", "FOUND", t, "e2"),       # replica drift
        ], [])
        self.assertEqual(idx.state_at("account_1", AUDIT).state, "FOUND")


class ScopeAbsence(unittest.TestCase):
    def test_snapshot_omission_proves_absence_of_deletable_record(self):
        sc = Scope("evidence_s", "account_service", "2026-09-14T00:00:00Z", {"account_9"},
                   captured_at="2026-09-14T00:00:00Z")
        idx = EvidenceIndex([], [sc])
        idx.system_of["account_1"] = "account_service"
        rs = idx.state_at("account_1", AUDIT)
        self.assertEqual((rs.state, rs.evidence_id), ("NOT_FOUND", "evidence_s"))
        self.assertEqual(ops(decide(rs, RULES, AUDIT)), (None, None, "VERIFY_RECORD_ABSENT"))

    def test_snapshot_omission_never_overrides_explicit_observation(self):
        sc = Scope("evidence_s", "account_service", "2026-09-15T00:00:00Z", set(),
                   captured_at="2026-09-15T00:00:00Z")
        idx = EvidenceIndex([ob("account_1", "account_service", "USER_PROFILE", "FOUND",
                                "2026-09-14T00:00:00Z", "evidence_o")], [sc])
        self.assertEqual(idx.state_at("account_1", AUDIT).state, "FOUND")

    def test_snapshot_omission_not_used_for_retained_records(self):
        sc = Scope("evidence_s", "security_service", "2026-09-14T00:00:00Z", set(),
                   captured_at="2026-09-14T00:00:00Z")
        idx = EvidenceIndex([], [sc])
        idx.system_of["audit_record_1"] = "security_service"
        self.assertIsNone(idx.state_at("audit_record_1", AUDIT).state)


class Policy(unittest.TestCase):
    def rs(self, rid, system, cat, state, raw=None):
        idx = EvidenceIndex([ob(rid, system, cat, state, "2026-09-14T00:00:00Z", "e", raw=raw)], [])
        return idx.state_at(rid, AUDIT)

    def test_retention_within_period_is_kept(self):
        d = decide(self.rs("audit_record_1", "security_service", "SECURITY_AUDIT_LOG", "FOUND",
                           {"retention_until": "2031-12-31T23:59:59Z"}), RULES, AUDIT)
        self.assertEqual(ops(d), (None, None, "VERIFY_RECORD_PRESENT"))

    def test_expired_retention_no_longer_protects_record(self):
        d = decide(self.rs("audit_record_1", "security_service", "SECURITY_AUDIT_LOG", "FOUND",
                           {"retention_until": "2026-01-01T00:00:00Z"}), RULES, AUDIT)
        self.assertEqual(d.remediation, "DELETE_RECORD")

    def test_active_recreation_paths(self):
        d = decide(self.rs("data_flow_1", "integration_service", "DATA_FLOW", "ACTIVE"), RULES, AUDIT)
        self.assertEqual(ops(d), ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"))
        d = decide(self.rs("backup_1", "backup_service", "BACKUP_ARCHIVE", "ACTIVE"), RULES, AUDIT)
        self.assertEqual(ops(d), ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"))
        d = decide(self.rs("backup_1", "backup_service", "BACKUP_ARCHIVE", "BLOCKED"), RULES, AUDIT)
        self.assertEqual(ops(d), (None, None, "VERIFY_RESTORE_BLOCKED"))

    def test_tombstone_keeps_reference_but_redacts_identifiers(self):
        d = decide(self.rs("tombstone_1", "identity_service", "ACCOUNT_TOMBSTONE", "IDENTIFIABLE"), RULES, AUDIT)
        self.assertEqual(ops(d), ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"))
        d = decide(self.rs("tombstone_1", "identity_service", "ACCOUNT_TOMBSTONE", "REDACTED"), RULES, AUDIT)
        self.assertEqual(ops(d), (None, None, "VERIFY_RECORD_REDACTED"))

    def test_analytics_anonymize_vs_marketing_delete(self):
        d = decide(self.rs("analytics_1", "analytics_platform", "ANALYTICS_PROFILE", "IDENTIFIABLE"), RULES, AUDIT)
        self.assertEqual(d.remediation, "ANONYMIZE_RECORD")
        d = decide(self.rs("analytics_1", "analytics_platform", "MARKETING_PROFILE", "FOUND"), RULES, AUDIT)
        self.assertEqual(d.remediation, "DELETE_RECORD")

    def test_retained_record_known_only_later_is_uncited_check(self):
        idx = EvidenceIndex([ob("audit_record_1", "security_service", "SECURITY_AUDIT_LOG", "FOUND",
                                "2026-09-20T00:00:00Z", "e_late", ["user_1"])], [])
        d = decide(idx.state_at("audit_record_1", AUDIT), RULES, AUDIT)
        self.assertEqual((d.verification, d.evidence_id), ("VERIFY_RECORD_PRESENT", ""))


class Linking(unittest.TestCase):
    def ev(self, aid, t, etype, target, rid, result="SUCCESS"):
        return ActivityEvent(aid, t, etype, "deletion_worker", target, rid, result, "")

    def test_workflow_assignment_is_one_to_one_and_status_aware(self):
        # Two cases reported at the same time; two identifier-less accounts.
        events = [self.ev("a1", "2026-09-05T11:00:00Z", "DELETE_RECORD", "account_service", "account_1", "FAILED"),
                  self.ev("a2", "2026-09-05T11:02:00Z", "DELETE_RECORD", "account_service", "account_2", "SUCCESS")]
        idx = EvidenceIndex([], [], [], events)
        out = idx.assign_workflow([
            CaseWindow("c_ok", "2026-09-05T12:00:00Z", AUDIT, set(), "COMPLETE"),
            CaseWindow("c_ns", "2026-09-05T12:00:00Z", AUDIT, set(), "NOT_STARTED")])
        self.assertEqual(set(out["c_ok"]), {"account_2"})
        self.assertEqual(set(out["c_ns"]), {"account_1"})

    def test_resource_with_someone_elses_identifier_is_not_assigned(self):
        events = [self.ev("a1", "2026-09-05T11:40:00Z", "ANONYMIZE_RECORD", "analytics_platform", "analytics_1")]
        idx = EvidenceIndex([ob("analytics_1", "analytics_platform", "ANALYTICS_PROFILE", "IDENTIFIABLE",
                                "2026-09-01T00:00:00Z", "e", ["other@x.com"])], [], [], events)
        out = idx.assign_workflow([CaseWindow("c", "2026-09-05T12:00:00Z", AUDIT)])
        self.assertEqual(out.get("c", {}), {})

    def test_stale_second_account_is_dropped(self):
        idx = EvidenceIndex([
            ob("account_old", "account_service", "USER_PROFILE", "NOT_FOUND", "2026-03-01T00:00:00Z", "e1", ["a@x.com"]),
            ob("account_new", "account_service", "USER_PROFILE", "FOUND", "2026-09-01T00:00:00Z", "e2", ["a@x.com"]),
        ], [])
        links = idx.direct_links(Identity("user_1", emails={"a@x.com"}))
        self.assertEqual(set(links), {"account_new"})


if __name__ == "__main__":
    unittest.main()
