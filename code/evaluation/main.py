"""Evaluation harness (Person B).

Runs the full pipeline on the 32 public sample requests, compares predictions
with ``dataset/sample_output.csv`` and reports:

  * case-level deletion-status accuracy;
  * step-level precision / recall / F1 (exact 5-tuple match: phase, system_id,
    resource_id, operation, evidence_id);
  * field-level accuracy (per-field equality over position-aligned steps);
  * exact-sequence accuracy (predicted step order == gold step order);
  * a per-case diff of missing / extra steps;
  * classifier agreement with the CLASSIFY_REQUEST workflow flags; and
  * any team-authored cases under ``code/evaluation/custom_cases/``.

It reproduces the verified sample-harness numbers before anything else:
status 31/32, exact-seq 24/32, step P=0.973, R=0.943.

Run:  python3 code/evaluation/main.py
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_DIR.parent
sys.path.insert(0, str(CODE_DIR))

from main import Pipeline  # noqa: E402

STEP_KEYS = ("phase", "system_id", "resource_id", "operation", "evidence_id")
FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def _key(step: dict) -> tuple:
    return tuple(step[k] for k in STEP_KEYS)


def evaluate_samples(pipeline: Pipeline, dataset_dir: Path):
    ds = Path(dataset_dir)
    reqs = list(csv.DictReader(open(ds / "sample_deletion_requests.csv",
                                     newline="", encoding="utf-8")))
    gold = {r["case_id"]: r for r in csv.DictReader(
        open(ds / "sample_output.csv", newline="", encoding="utf-8"))}

    status_ok = seq_ok = 0
    tp = fp = fn = 0
    field_hits = field_total = 0
    diffs = []

    for r in reqs:
        c = r["case_id"]
        status, steps = pipeline.predict(c)
        g = gold[c]
        gold_steps = json.loads(g["steps_json"])

        status_ok += int(status == g["deletion_status"])

        P = [_key(s) for s in steps]
        G = [_key(s) for s in gold_steps]
        seq_ok += int(P == G)
        ps, gs = set(P), set(G)
        tp += len(ps & gs)
        fp += len(ps - gs)
        fn += len(gs - ps)

        # field-level accuracy over position-aligned steps
        for a, b in zip(steps, gold_steps):
            for f in FIELDS:
                field_total += 1
                field_hits += int(a.get(f) == b.get(f))
        field_total += abs(len(steps) - len(gold_steps)) * len(FIELDS)

        if P != G:
            diffs.append((c, status, g["deletion_status"],
                          sorted(gs - ps), sorted(ps - gs)))

    n = len(reqs)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    field_acc = field_hits / field_total if field_total else 1.0

    return {
        "n": n,
        "status_ok": status_ok,
        "seq_ok": seq_ok,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "field_acc": field_acc,
        "tp": tp, "fp": fp, "fn": fn,
        "diffs": diffs,
    }


def run_custom_cases():
    """Discover and run team-authored cases in custom_cases/.

    Each *.py module may expose ``run() -> (name: str, passed: bool, detail: str)``
    or ``CASES = [ (name, passed, detail), ... ]``. Degrades to [] when the
    directory is absent or empty.
    """
    results = []
    cdir = Path(__file__).resolve().parent / "custom_cases"
    if not cdir.is_dir():
        return results
    if str(cdir) not in sys.path:
        sys.path.insert(0, str(cdir))
    for path in sorted(cdir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"custom_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)  # type: ignore
        except Exception as e:  # a broken case is a failure, reported loudly
            results.append((path.stem, False, f"import error: {e}"))
            continue
        if hasattr(mod, "run"):
            try:
                results.append(tuple(mod.run()))
            except Exception as e:
                results.append((path.stem, False, f"run() error: {e}"))
        if hasattr(mod, "CASES"):
            for row in mod.CASES:
                results.append(tuple(row))
    return results


def main() -> int:
    ds = REPO_ROOT / "dataset"
    pipeline = Pipeline(ds)
    m = evaluate_samples(pipeline, ds)

    print("=" * 64)
    print("SAMPLE EVALUATION (32 public cases)")
    print("=" * 64)
    print(f"status accuracy      : {m['status_ok']}/{m['n']} "
          f"({m['status_ok'] / m['n']:.3f})")
    print(f"exact-sequence match : {m['seq_ok']}/{m['n']} "
          f"({m['seq_ok'] / m['n']:.3f})")
    print(f"step precision       : {m['precision']:.3f}  "
          f"(tp={m['tp']} fp={m['fp']} fn={m['fn']})")
    print(f"step recall          : {m['recall']:.3f}")
    print(f"step F1              : {m['f1']:.3f}")
    print(f"field-level accuracy : {m['field_acc']:.3f}")

    print("\nper-case diffs (sequence mismatches):")
    for c, pred, gold_s, missing, extra in m["diffs"]:
        print(f"  {c} pred={pred} gold={gold_s}")
        for s in missing:
            print("    MISSING", s)
        for s in extra:
            print("    EXTRA  ", s)
        if not missing and not extra:
            print("    (order differs only)")

    na, flagged, agree = pipeline.classifier_crosscheck()
    print("\nclassifier cross-check vs CLASSIFY_REQUEST flags (full set):")
    print(f"  classifier NOT_APPLICABLE : {len(na)}")
    print(f"  workflow flags            : {len(flagged)}")
    print(f"  agreement                 : {len(agree)}/{len(flagged)}")
    disagree = (na | flagged) - (na & flagged)
    if disagree:
        print(f"  disagreements             : {sorted(disagree)}")

    custom = run_custom_cases()
    print("\n" + "=" * 64)
    print(f"TEAM-AUTHORED CASES ({len(custom)})")
    print("=" * 64)
    passed = 0
    for name, ok, detail in custom:
        passed += int(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if custom:
        print(f"  {passed}/{len(custom)} passed")

    # Baseline guard: the deterministic core must not regress.
    assert m["status_ok"] == 31, f"status regressed: {m['status_ok']}/32"
    assert m["seq_ok"] == 24, f"exact-seq regressed: {m['seq_ok']}/32"
    assert round(m["precision"], 3) >= 0.973, f"precision regressed: {m['precision']}"
    assert round(m["recall"], 3) >= 0.943, f"recall regressed: {m['recall']}"
    if custom and passed != len(custom):
        return 1
    print("\nbaseline guard: OK (31/32 status, 24/32 seq, P>=0.973, R>=0.943)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
