"""Sanity analysis of a full run over dataset/deletion_requests.csv (not part
of the prediction path). Uses the same wiring as tools/sample_harness.py; no
classifier, so every request is treated as a deletion request here.

    python3 tools/full_run_report.py [repo_root]
"""
import csv
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
HARNESS = (REPO / "tools" / "sample_harness.py").read_text().split("if __name__")[0]


def main():
    ns = {"__name__": "harness"}
    argv, sys.argv = sys.argv, ["harness", str(REPO)]
    try:
        exec(compile(HARNESS, "sample_harness.py", "exec"), ns)
    finally:
        sys.argv = argv
    run_case, DIRECT, WF, idx = ns["run_case"], ns["DIRECT"], ns["WF"], ns["idx"]
    by_email, people, norm_email = ns["by_email"], ns["people"], ns["norm_email"]
    reqs = list(csv.DictReader(open(REPO / "dataset" / "deletion_requests.csv")))

    status = Counter()
    n_steps, n_resources = {}, {}
    findings = Counter()
    zero_res, no_account, no_wf = [], [], []
    owner = defaultdict(set)          # resource -> {(case, user)}
    uncited_complete = []
    for r in reqs:
        c = r["case_id"]
        st, steps, why = run_case(r)
        status[st] += 1
        n_steps[c] = len(steps)
        res = {s["resource_id"] for s in steps}
        n_resources[c] = len(res)
        uid = by_email.get(norm_email(r["email"]))
        for x in set(DIRECT.get(c, {})) | set(WF.get(c, {})):
            owner[x].add((c, uid))
        if not res:
            zero_res.append(c)
        if not any(idx.category_of(x) == "USER_PROFILE" for x in res):
            no_account.append(c)
        if not WF.get(c):
            no_wf.append(c)
        for s in steps:
            if s["phase"] == "FINDING":
                findings[s["operation"]] += 1
            if st == "COMPLETE" and not s["evidence_id"]:
                uncited_complete.append((c, s["resource_id"]))

    multi_person = {x: sorted(o) for x, o in owner.items() if len({u for _, u in o}) > 1}
    multi_case = {x: sorted(o) for x, o in owner.items() if len(o) > 1 and x not in multi_person}

    print(f"requests: {len(reqs)}")
    print(f"status distribution: {dict(status)}")
    v = list(n_steps.values())
    print(f"steps per case: min {min(v)}, median {statistics.median(v)}, mean {statistics.mean(v):.2f}, max {max(v)}")
    print(f"steps histogram: {dict(sorted(Counter(v).items()))}")
    print(f"FINDING counts by operation: {dict(findings)}")
    print(f"cases with zero resources ({len(zero_res)}): {zero_res}")
    print(f"cases with no USER_PROFILE resource ({len(no_account)}): {no_account}")
    print(f"cases where assign_workflow gave no candidate ({len(no_wf)}); "
          f"of these without any USER_PROFILE: {sorted(set(no_wf) & set(no_account))}")
    print(f"resources linked to more than one PERSON ({len(multi_person)}):")
    for x, o in sorted(multi_person.items()):
        print(f"   {x}: {o}")
    print(f"resources shared by several requests of the SAME person ({len(multi_case)}): "
          f"{sorted(multi_case)[:15]}{' ...' if len(multi_case) > 15 else ''}")
    print(f"COMPLETE-case steps without evidence (must be 0): {uncited_complete}")


if __name__ == "__main__":
    main()
