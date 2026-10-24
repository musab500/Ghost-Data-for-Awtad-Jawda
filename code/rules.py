"""Policy engine: deletion_rules.csv + resource state -> required steps.

Each rule's required_action (DELETE / REDACT / ANONYMIZE / RETAIN) fixes the
compliant end state of a record. Data flows and restorable backups are
recreation paths: they must be DISABLED / BLOCKED (BACKUP_ARCHIVE is RETAIN,
with the condition "Restore access can be blocked").

For one resource, ``decide`` returns the finding (if the current state
violates policy), the remediation that fixes it, and the verification that
must hold afterwards (or that proves compliance now).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from models import (ACTIVE, ANONYMIZED, BLOCKED, DISABLED, FOUND, IDENTIFIABLE,
                    NOT_FOUND, REDACTED, parse_ts)
from state import ResourceState

# Operation order inside each phase (problem statement, "Allowed Operations").
OPERATION_ORDER = [
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW",
    "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD",
    "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
]
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED",
    "ANONYMIZE_RECORD": "ANONYMIZED", "VERIFY_RECORD_ABSENT": "ABSENT",
    "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass
class Decision:
    system_id: str
    resource_id: str
    rule_id: str
    finding: str | None        # FINDING operation, None if compliant
    remediation: str | None    # REMEDIATION operation
    verification: str | None   # VERIFICATION operation
    evidence_id: str           # evidence for the finding / current-state proof
    note: str = ""

    @property
    def violates(self) -> bool:
        return self.finding is not None


def load_rules(dataset_dir: Path) -> dict[str, Rule]:
    rules = {}
    with open(Path(dataset_dir) / "deletion_rules.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rule = Rule(r["rule_id"], r["data_category"], r["condition"], r["required_action"])
            if rule.data_category in rules:
                raise ValueError(f"duplicate rule for {rule.data_category}")
            rules[rule.data_category] = rule
    return rules


def _retention_active(rs: ResourceState, audit_at: str) -> bool:
    """RETAIN rules apply only while the supplied retention / hold is active.
    Records without a supplied end date are treated as within retention."""
    for key in ("retention_until", "retained_until"):
        v = rs.raw.get(key) if rs.raw else None
        if v and v not in ("null", "None"):
            return parse_ts(v) >= parse_ts(audit_at)
    return True


def decide(rs: ResourceState, rules: dict[str, Rule], audit_at: str) -> Decision | None:
    """Policy decision for one resource; None when no rule applies or the
    resource has no usable evidence at audit_at."""
    cat, st = rs.category, rs.state
    if cat is None:
        return None
    if st is None:
        # A retained record linked to the person but only evidenced after
        # audit_at: its continued presence is a post-remediation check (no
        # citation is possible, so it can only appear in INCOMPLETE plans).
        rule = rules.get(cat)
        if rule and rule.required_action == "RETAIN" and cat != "BACKUP_ARCHIVE" \
                and rs.raw.get("later_evidence"):
            return Decision(rs.system_id, rs.resource_id, rule.rule_id, None, None,
                            "VERIFY_RECORD_PRESENT", "", "evidenced only after audit_at")
        return None
    mk = lambda rule_id, f, r, v, note="": Decision(  # noqa: E731
        rs.system_id, rs.resource_id, rule_id, f, r, v, rs.evidence_id, note)

    if cat == "DATA_FLOW":
        if st == ACTIVE:
            return mk("recreation_path", "REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW",
                      "VERIFY_DATA_FLOW_DISABLED")
        if st == DISABLED:
            return mk("recreation_path", None, None, "VERIFY_DATA_FLOW_DISABLED")
        return None

    rule = rules.get(cat)
    if rule is None:
        return None
    action = rule.required_action

    if cat == "BACKUP_ARCHIVE":
        if st == ACTIVE:
            return mk(rule.rule_id, "REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED")
        if st == BLOCKED:
            return mk(rule.rule_id, None, None, "VERIFY_RESTORE_BLOCKED")
        return None

    if action == "RETAIN":
        if not _retention_active(rs, audit_at):
            action = "DELETE"     # retention expired: no exception applies
        elif st == NOT_FOUND:
            return mk(rule.rule_id, None, None, None, "retained record missing")
        else:
            return mk(rule.rule_id, None, None, "VERIFY_RECORD_PRESENT")

    if action == "DELETE":
        if st == NOT_FOUND:
            return mk(rule.rule_id, None, None, "VERIFY_RECORD_ABSENT")
        if st in (FOUND, IDENTIFIABLE, ACTIVE):
            return mk(rule.rule_id, "REPORT_REMAINING_RECORD", "DELETE_RECORD",
                      "VERIFY_RECORD_ABSENT")
        # Redacted / anonymised copies of a record that must be deleted.
        return mk(rule.rule_id, "REPORT_REMAINING_RECORD", "DELETE_RECORD",
                  "VERIFY_RECORD_ABSENT", f"state {st} still a copy")

    if action == "REDACT":
        if st == REDACTED or st == ANONYMIZED:
            return mk(rule.rule_id, None, None, "VERIFY_RECORD_REDACTED")
        if st in (IDENTIFIABLE, FOUND):
            return mk(rule.rule_id, "REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED")
        return None   # NOT_FOUND: nothing left to redact

    if action == "ANONYMIZE":
        if st == ANONYMIZED or st == REDACTED:
            return mk(rule.rule_id, None, None, "VERIFY_RECORD_ANONYMIZED")
        if st in (IDENTIFIABLE, FOUND):
            return mk(rule.rule_id, "REPORT_EXPOSED_PII", "ANONYMIZE_RECORD",
                      "VERIFY_RECORD_ANONYMIZED")
        return None

    raise ValueError(f"unsupported required_action {action!r} in {rule.rule_id}")
