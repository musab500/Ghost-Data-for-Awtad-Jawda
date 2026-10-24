"""Ablation of the workflow-assignment cost terms (not part of the prediction
path). For each term, switch it off, rebuild the sample harness, and score it
against dataset/sample_output.csv.

    python3 tools/ablation.py [repo_root]
"""
import csv
import json
import sys
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(REPO / "code"))
import state  # noqa: E402

HARNESS = (REPO / "tools" / "sample_harness.py").read_text().split("if __name__")[0]
DEFAULTS = {k: getattr(state, k) for k in (
    "CITATION_TIER_PENALTY", "SLOT_TOLERANCE_MIN", "RUN_AFFINITY_BONUS",
    "FAILED_RUN_PENALTY", "STATUS_MISMATCH_PENALTY")}
ORIG_DIRECT = state.EvidenceIndex.direct_links


def no_request_windows(self, identity, not_before=None, audit_at=None, has_later_request=False):
    """Ablation: ignore the person's other requests (both window boundaries)."""
    return ORIG_DIRECT(self, identity, None, audit_at, False)


ABLATIONS = [
    ("full model", {}, None),
    ("- evidence-tier cost", {"CITATION_TIER_PENALTY": (0.0, 0.0, 0.0)}, None),
    ("- slot occupancy", {"SLOT_TOLERANCE_MIN": -1.0}, None),
    ("- run affinity", {"RUN_AFFINITY_BONUS": 0.0}, None),
    ("- failed-run penalty", {"FAILED_RUN_PENALTY": 0.0}, None),
    ("- all three run-shape terms", {"SLOT_TOLERANCE_MIN": -1.0, "RUN_AFFINITY_BONUS": 0.0,
                                     "FAILED_RUN_PENALTY": 0.0}, None),
    ("- status/result tie-break", {"STATUS_MISMATCH_PENALTY": 0.0}, None),
    ("- per-person request windows", {}, no_request_windows),
]


def score() -> dict:
    ns = {"__name__": "harness"}
    argv, sys.argv = sys.argv, ["harness", str(REPO)]
    try:
        exec(compile(HARNESS, "sample_harness.py", "exec"), ns)
    finally:
        sys.argv = argv
    ds = REPO / "dataset"
    gold = {r["case_id"]: r for r in csv.DictReader(open(ds / "sample_output.csv"))}
    st = seq = tp = fp = fn = 0
    reqs = list(csv.DictReader(open(ds / "sample_deletion_requests.csv")))
    for req in reqs:
        status, steps, _ = ns["run_case"](req)
        g = gold[req["case_id"]]
        key = lambda s: (s["phase"], s["system_id"], s["resource_id"], s["operation"], s["evidence_id"])  # noqa: E731
        P = [key(s) for s in steps]
        G = [key(s) for s in json.loads(g["steps_json"])]
        st += status == g["deletion_status"]
        seq += P == G
        tp += len(set(P) & set(G)); fp += len(set(P) - set(G)); fn += len(set(G) - set(P))
    return {"n": len(reqs), "status": st, "seq": seq, "P": tp / (tp + fp), "R": tp / (tp + fn)}


def main():
    rows = []
    for name, patch, direct in ABLATIONS:
        for k, v in DEFAULTS.items():
            setattr(state, k, v)
        for k, v in patch.items():
            setattr(state, k, v)
        state.EvidenceIndex.direct_links = direct or ORIG_DIRECT
        rows.append((name, score()))
    state.EvidenceIndex.direct_links = ORIG_DIRECT
    # Sensitivity: scale each weight by 0.5 and 2 (others at default).
    for k in ("CITATION_TIER_PENALTY", "RUN_AFFINITY_BONUS", "FAILED_RUN_PENALTY",
              "STATUS_MISMATCH_PENALTY", "SLOT_TOLERANCE_MIN"):
        for f in (0.5, 2.0):
            for kk, v in DEFAULTS.items():
                setattr(state, kk, v)
            v = DEFAULTS[k]
            setattr(state, k, tuple(x * f for x in v) if isinstance(v, tuple) else v * f)
            rows.append((f"{k} x{f:g}", score()))
    for kk, v in DEFAULTS.items():
        setattr(state, kk, v)
    base = rows[0][1]
    print(f"| Configuration | Status | Exact sequence | Step P | Step R | Δ exact |")
    print(f"|---|---|---|---|---|---|")
    for name, m in rows:
        print(f"| {name} | {m['status']}/{m['n']} | {m['seq']}/{m['n']} | {m['P']:.3f} | {m['R']:.3f} | "
              f"{m['seq'] - base['seq']:+d} |")


if __name__ == "__main__":
    main()
