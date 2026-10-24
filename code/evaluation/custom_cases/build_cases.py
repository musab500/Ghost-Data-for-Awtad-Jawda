"""Builds the three team-made evaluation cases as self-contained mini datasets.

Each case directory has the same layout as dataset/ (deletion_requests.csv,
user_identities.csv, user_audit_log.csv, system_activity.csv,
deletion_rules.csv, evidence_files.csv, evidence/...) plus
expected_output.csv, written by hand from the policy and the output contract
(not produced by the pipeline). Any pipeline that accepts a dataset directory
can run them unchanged.

    python3 code/evaluation/custom_cases/build_cases.py
"""
import csv
import hashlib
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES = HERE.parents[2] / "dataset" / "deletion_rules.csv"
REQ_HEADER = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]
DELETE_TEXT = "Please permanently delete my account and all personal data linked to this email address."


def step(n, phase, system, rid, op, state, ev=""):
    return {"step_no": n, "phase": phase, "system_id": system, "resource_id": rid,
            "operation": op, "state": state, "evidence_id": ev}


def handover(ts, lines):
    rows = "\n".join(f"- {ts} | {s} | {r} | {c} | {st} | {i}" for s, r, c, st, i in lines)
    return f"CUSTOMER PLATFORM - SHIFT HANDOVER\nSnapshot: {ts}\n\nChecks:\n\n{rows}\n"


def write_case(name, user, request, evidence, expected, activity=()):
    d = HERE / name
    if d.exists():
        shutil.rmtree(d)
    (d / "evidence" / "records").mkdir(parents=True)
    uid, full, email, phone = user
    with open(d / "user_identities.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["user_id", "name", "email", "phone", "email_hash", "updated_at"])
        w.writerow([uid, full, email, phone, hashlib.sha256(email.encode()).hexdigest(), "2026-01-01T00:00:00Z"])
    with open(d / "user_audit_log.csv", "w", newline="") as f:
        csv.writer(f).writerow(["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"])
    with open(d / "system_activity.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["activity_id", "event_time", "event_type", "source_system", "target_system",
                    "resource_id", "reported_result", "details"])
        w.writerows(activity)
    shutil.copy(RULES, d / "deletion_rules.csv")
    with open(d / "deletion_requests.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(REQ_HEADER)
        w.writerow([request[k] for k in REQ_HEADER])
    with open(d / "evidence_files.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file_id", "file_path", "origin", "captured_at"])
        for fid, rel, origin, captured, content in evidence:
            w.writerow([fid, rel, origin, captured])
            (d / rel).write_text(content, encoding="utf-8")
    with open(d / "expected_output.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        w.writerow([request["case_id"], expected[0],
                    json.dumps(expected[1], ensure_ascii=False, separators=(",", ":"), sort_keys=True)])


def case_a_temporal():
    email = "alice.cutoff@example.com"
    write_case(
        "a_temporal_cutoff", ("user_901", "Alice Cutoff", email, "+44 7700 900901"),
        {"case_id": "custom_a", "email": email, "requested_at": "2026-09-01T09:00:00Z",
         "reported_status": "COMPLETE", "reported_at": "2026-09-02T09:00:00Z",
         "audit_at": "2026-09-10T12:00:00Z", "request_text": DELETE_TEXT},
        [("evidence_01", "evidence/records/handover_20260905.txt", "customer_account_service",
          "2026-09-05T12:00:00Z", handover("2026-09-05T12:00:00Z", [
              ("account_service", "account_9001", "USER_PROFILE", "NOT_FOUND", email),
              ("search_service", "search_9001", "SEARCH_INDEX_ENTRY", "FOUND", email),
              ("security_service", "audit_record_9001", "SECURITY_AUDIT_LOG", "FOUND", "user_901")])),
         # Captured AFTER audit_at: shows the search entry gone. Must be ignored.
         ("evidence_02", "evidence/records/handover_20260920.txt", "customer_account_service",
          "2026-09-20T12:00:00Z", handover("2026-09-20T12:00:00Z", [
              ("search_service", "search_9001", "SEARCH_INDEX_ENTRY", "NOT_FOUND", email)]))],
        ("INCOMPLETE", [
            step(1, "FINDING", "search_service", "search_9001", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_01"),
            step(2, "REMEDIATION", "search_service", "search_9001", "DELETE_RECORD", "ABSENT"),
            step(3, "VERIFICATION", "account_service", "account_9001", "VERIFY_RECORD_ABSENT", "ABSENT"),
            step(4, "VERIFICATION", "search_service", "search_9001", "VERIFY_RECORD_ABSENT", "ABSENT"),
            step(5, "VERIFICATION", "security_service", "audit_record_9001", "VERIFY_RECORD_PRESENT", "PRESENT")]))


def case_b_recreation():
    email = "bob.recreate@example.com"
    write_case(
        "b_recreation_path", ("user_902", "Bob Recreate", email, "+44 7700 900902"),
        {"case_id": "custom_b", "email": email, "requested_at": "2026-09-01T09:00:00Z",
         "reported_status": "COMPLETE", "reported_at": "2026-09-02T09:00:00Z",
         "audit_at": "2026-09-10T12:00:00Z", "request_text": DELETE_TEXT},
        [("evidence_01", "evidence/records/handover_20260905.txt", "customer_account_service",
          "2026-09-05T12:00:00Z", handover("2026-09-05T12:00:00Z", [
              ("account_service", "account_9002", "USER_PROFILE", "NOT_FOUND", email),
              ("search_service", "search_9002", "SEARCH_INDEX_ENTRY", "NOT_FOUND", email),
              ("export_service", "export_9002", "USER_EXPORT", "FOUND", email),
              ("integration_service", "data_flow_9002", "DATA_FLOW", "ACTIVE", email),
              ("backup_service", "backup_9002", "BACKUP_ARCHIVE", "ACTIVE", email),
              ("security_service", "audit_record_9002", "SECURITY_AUDIT_LOG", "FOUND", "user_902")]))],
        ("INCOMPLETE", [
            step(1, "FINDING", "export_service", "export_9002", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_01"),
            step(2, "FINDING", "integration_service", "data_flow_9002", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE", "evidence_01"),
            step(3, "FINDING", "backup_service", "backup_9002", "REPORT_RESTORE_RISK", "ACTIVE", "evidence_01"),
            # Recreation paths are neutralised before the dependent record is deleted.
            step(4, "REMEDIATION", "integration_service", "data_flow_9002", "DISABLE_DATA_FLOW", "DISABLED"),
            step(5, "REMEDIATION", "backup_service", "backup_9002", "BLOCK_RESTORE", "BLOCKED"),
            step(6, "REMEDIATION", "export_service", "export_9002", "DELETE_RECORD", "ABSENT"),
            step(7, "VERIFICATION", "account_service", "account_9002", "VERIFY_RECORD_ABSENT", "ABSENT"),
            step(8, "VERIFICATION", "export_service", "export_9002", "VERIFY_RECORD_ABSENT", "ABSENT"),
            step(9, "VERIFICATION", "search_service", "search_9002", "VERIFY_RECORD_ABSENT", "ABSENT"),
            step(10, "VERIFICATION", "security_service", "audit_record_9002", "VERIFY_RECORD_PRESENT", "PRESENT"),
            step(11, "VERIFICATION", "integration_service", "data_flow_9002", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
            step(12, "VERIFICATION", "backup_service", "backup_9002", "VERIFY_RESTORE_BLOCKED", "BLOCKED")]))


def case_c_retention():
    email = "carol.retain@example.com"
    records = {"exported_at": "2026-09-05T12:00:00Z", "origin": "records_assurance", "records": [
        {"recorded_at": "2026-09-05T12:00:00Z", "system": "account_service", "resource_id": "account_9003",
         "record_type": "USER_PROFILE", "result": "NOT_FOUND", "subject_reference": email},
        {"recorded_at": "2026-09-05T12:00:00Z", "system": "security_service", "resource_id": "audit_record_9003",
         "record_type": "SECURITY_AUDIT_LOG", "result": "FOUND", "subject_reference": "user_903",
         "retention_until": "2031-12-31T23:59:59Z"},
        {"recorded_at": "2026-09-05T12:00:00Z", "system": "identity_service", "resource_id": "tombstone_9003",
         "record_type": "ACCOUNT_TOMBSTONE", "result": "REDACTED", "subject_reference": "user_903"}]}
    write_case(
        "c_retention", ("user_903", "Carol Retain", email, "+44 7700 900903"),
        {"case_id": "custom_c", "email": email, "requested_at": "2026-09-01T09:00:00Z",
         "reported_status": "COMPLETE", "reported_at": "2026-09-02T09:00:00Z",
         "audit_at": "2026-09-10T12:00:00Z", "request_text": DELETE_TEXT},
        [("evidence_01", "evidence/records/records_check_20260905.json", "records_assurance",
          "2026-09-05T12:00:00Z", json.dumps(records, indent=2))],
        ("COMPLETE", [
            step(1, "VERIFICATION", "account_service", "account_9003", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_01"),
            # Security audit log within retention: verified present, never deleted.
            step(2, "VERIFICATION", "security_service", "audit_record_9003", "VERIFY_RECORD_PRESENT", "PRESENT", "evidence_01"),
            step(3, "VERIFICATION", "identity_service", "tombstone_9003", "VERIFY_RECORD_REDACTED", "REDACTED", "evidence_01")]),
        # A workflow claim that the audit record was deleted must not override policy or evidence.
        activity=[["activity_1", "2026-09-02T08:30:00Z", "DELETE_RECORD", "deletion_worker", "security_service",
                   "audit_record_9003", "SUCCESS", "Deletion worker reported the requested operation as completed."]])


if __name__ == "__main__":
    case_a_temporal()
    case_b_recreation()
    case_c_retention()
    print("built:", sorted(p.name for p in HERE.iterdir() if p.is_dir()))
