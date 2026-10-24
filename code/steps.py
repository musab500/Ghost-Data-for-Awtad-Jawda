"""Step assembly (Person B).

Turns a case's list of per-resource ``Decision`` objects (from ``rules.decide``)
into the ordered ``steps_json`` array the output contract requires. This is the
exact logic verified in ``tools/sample_harness.py``, lifted into a module.

Ordering (problem_statement.md §Rules):
    phase  : FINDING, then REMEDIATION, then VERIFICATION
    within : (OPERATION_ORDER.index(operation), system_id, resource_id) ascending

Evidence citation (problem_statement.md §Rules):
    * FINDING steps always cite the decision's evidence_id.
    * For a COMPLETE case, every step is a VERIFICATION step citing the
      evidence that proves the current compliant state; a decision whose
      evidence_id is empty cannot be proven and is dropped.
    * For an INCOMPLETE case, REMEDIATION and post-remediation VERIFICATION
      steps describe future work, so their evidence_id is "".
"""
from __future__ import annotations

from rules import OPERATION_ORDER, OP_STATE


def status_for(decisions) -> str:
    """INCOMPLETE if any decision reports a finding (a policy violation),
    otherwise COMPLETE. (NOT_APPLICABLE is decided upstream by the classifier.)"""
    return "INCOMPLETE" if any(d.violates for d in decisions) else "COMPLETE"


def assemble_steps(decisions, status: str) -> list[dict]:
    """Build the ordered step list for one case from its decisions."""
    incomplete = status == "INCOMPLETE"
    steps: list[dict] = []
    for phase, attr in (("FINDING", "finding"),
                        ("REMEDIATION", "remediation"),
                        ("VERIFICATION", "verification")):
        # A COMPLETE case has verification steps only.
        if phase != "VERIFICATION" and not incomplete:
            continue
        rows = [(OPERATION_ORDER.index(getattr(d, attr)), d.system_id,
                 d.resource_id, getattr(d, attr), d)
                for d in decisions if getattr(d, attr)]
        for _, system_id, resource_id, op, d in sorted(rows, key=lambda x: x[:3]):
            # COMPLETE verification steps must cite evidence; drop uncitable ones.
            if not incomplete and not d.evidence_id:
                continue
            evidence_id = d.evidence_id if (phase == "FINDING" or not incomplete) else ""
            steps.append({
                "step_no": len(steps) + 1,
                "phase": phase,
                "system_id": system_id,
                "resource_id": resource_id,
                "operation": op,
                "state": OP_STATE[op],
                "evidence_id": evidence_id,
            })
    return steps
