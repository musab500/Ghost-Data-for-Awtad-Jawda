"""Scratch end-to-end harness for Person A's state.py/rules.py.
Minimal identity + step assembly stand-ins (Person B owns the real ones).
Usage: harness.py REPO [case_id ...]"""
import csv, hashlib, json, sys
from collections import defaultdict
from pathlib import Path

repo = Path(sys.argv[1]); only = set(sys.argv[2:])
sys.path.insert(0, str(repo / "code"))
from models import Identity, norm_email, norm_phone, norm_id
from parsers import load_evidence
from parsers.activity import parse_activity
from state import EvidenceIndex, CaseWindow
from rules import load_rules, decide, OPERATION_ORDER, OP_STATE

import os
ds = Path(os.environ["HARNESS_DATASET"]) if os.environ.get("HARNESS_DATASET") else repo / "dataset"
obs, scopes, skipped = load_evidence(ds)
events, aobs, links = parse_activity(ds)
from parsers.activity import resource_aliases
from multimodal import multimodal_observations
import io
obs += multimodal_observations(skipped, resource_aliases(events), log=io.StringIO())
idx = EvidenceIndex(obs + aobs, scopes, links, events)
rules = load_rules(ds)

ids = list(csv.DictReader(open(ds / "user_identities.csv")))
audit = list(csv.DictReader(open(ds / "user_audit_log.csv")))
people = defaultdict(lambda: Identity(""))
for r in ids:
    p = people[r["user_id"]]; p.user_id = r["user_id"].lower()
    p.emails.add(norm_email(r["email"])); p.phones.add(norm_phone(r["phone"]))
    p.email_hashes.add(r["email_hash"].lower()); p.names.add(r["name"])
for a in audit:
    p = people[a["user_id"]]; p.user_id = a["user_id"].lower()
    for v in (a["old_value"], a["new_value"]):
        if not v: continue
        if a["field_name"] == "email": p.emails.add(norm_email(v))
        elif a["field_name"] == "phone": p.phones.add(norm_phone(v))
        elif a["field_name"] == "email_hash": p.email_hashes.add(v.lower())
for p in people.values():
    p.email_hashes |= {hashlib.sha256(e.encode()).hexdigest() for e in p.emails}
by_email = {}
for uid, p in people.items():
    for e in p.emails: by_email.setdefault(e, uid)

ALL = [r for f in ("sample_deletion_requests.csv", "deletion_requests.csv") if (ds / f).exists()
       for r in csv.DictReader(open(ds / f))]
assert len({r["case_id"] for r in ALL}) == len(ALL)
from state import RequestRef
DIRECT = idx.link_requests([RequestRef(r["case_id"], people.get(by_email.get(norm_email(r["email"]))),
                                       r["audit_at"], r["requested_at"]) for r in ALL])

def expects_complete(r):
    decs = [decide(idx.state_at(x, r["audit_at"]), rules, r["audit_at"]) for x in DIRECT[r["case_id"]]]
    return not any(d and d.violates for d in decs)
WF = idx.assign_workflow([CaseWindow(r["case_id"], r["reported_at"], r["audit_at"],
                          {idx.category_of(x) for x in DIRECT[r["case_id"]]}, r["reported_status"],
                          expects_complete(r), set(DIRECT[r["case_id"]])) for r in ALL])

def run_case(req):
    uid = by_email.get(norm_email(req["email"]))
    if uid is None:
        return "INCOMPLETE", [], "no identity"
    linked = {k: list(v) for k, v in DIRECT[req["case_id"]].items()}
    for rid, why in WF.get(req["case_id"], {}).items():
        linked.setdefault(rid, []).append(why)
    decs = []
    for rid, reasons in sorted(linked.items()):
        rs = idx.state_at(rid, req["audit_at"])
        d = decide(rs, rules, req["audit_at"])
        if d: decs.append((d, reasons))
    incomplete = any(d.violates for d, _ in decs)
    steps = []
    for phase, attr in (("FINDING", "finding"), ("REMEDIATION", "remediation"), ("VERIFICATION", "verification")):
        if phase != "VERIFICATION" and not incomplete: continue
        rows = [(OPERATION_ORDER.index(getattr(d, attr)), d.system_id, d.resource_id, getattr(d, attr), d)
                for d, _ in decs if getattr(d, attr)]
        for _, sysid, rid, op, d in sorted(rows, key=lambda x: x[:3]):
            if not incomplete and not d.evidence_id: continue  # COMPLETE steps must cite
            ev = d.evidence_id if (phase == "FINDING" or not incomplete) else ""
            steps.append({"step_no": len(steps) + 1, "phase": phase, "system_id": sysid,
                          "resource_id": rid, "operation": op, "state": OP_STATE[op], "evidence_id": ev})
    return ("INCOMPLETE" if incomplete else "COMPLETE"), steps, {d.resource_id: r for d, r in decs}

if __name__ == "__main__":
    reqs = list(csv.DictReader(open(ds / "sample_deletion_requests.csv")))
    gold = {r["case_id"]: r for r in csv.DictReader(open(ds / "sample_output.csv"))}
    st_ok = seq_ok = 0; tp = fp = fn = 0
    for req in reqs:
        c = req["case_id"]
        if only and c not in only: continue
        status, steps, why = run_case(req)
        g = gold[c]; gs = json.loads(g["steps_json"])
        key = lambda s: (s["phase"], s["system_id"], s["resource_id"], s["operation"], s["evidence_id"])
        P = [key(s) for s in steps]; G = [key(s) for s in gs]
        st_ok += status == g["deletion_status"]; seq_ok += P == G
        tp += len(set(P) & set(G)); fp += len(set(P) - set(G)); fn += len(set(G) - set(P))
        if P != G or only:
            print(f"== {c} pred={status} gold={g['deletion_status']}")
            for s in sorted(set(G) - set(P)): print("   MISSING", s)
            for s in sorted(set(P) - set(G)): print("   EXTRA  ", s, why.get(s[2], "") if isinstance(why, dict) else why)
            if set(P) == set(G): print("   (order differs)")
    n = len(only) if only else len(reqs)
    print(f"status {st_ok}/{n}  exact-seq {seq_ok}/{n}  step P={tp/(tp+fp+1e-9):.3f} R={tp/(tp+fn+1e-9):.3f}")
