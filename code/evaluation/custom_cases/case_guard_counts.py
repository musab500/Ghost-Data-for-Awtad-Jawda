"""Custom case 5 (regression): the sample guard compares step counts, not rounded floats.

The baseline is tp=186, fp=4, fn=7 (precision 186/190 = 0.97895, which rounds to 0.979 but is
below 0.979). It must PASS at the baseline and FAIL on a one-step regression in either direction.
"""
import importlib.util
from pathlib import Path


def run():
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("eval_main_for_guard", path)
    em = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(em)
    base = {"n": 32, "status_ok": em.MIN_STATUS_OK, "seq_ok": em.MIN_EXACT_SEQ,
            "tp": 186, "fp": 4, "fn": 7, "precision": 186 / 190, "recall": 186 / 193}
    cases = {
        "baseline passes": (base, 0),
        "tp-1 fails": ({**base, "tp": 185}, 1),
        "fp+1 fails": ({**base, "fp": 5}, 1),
        "fn+1 fails": ({**base, "fn": 8}, 1),
        "better passes": ({**base, "tp": 190, "fp": 2, "fn": 3}, 0),
        "status-1 fails": ({**base, "status_ok": em.MIN_STATUS_OK - 1}, 1),
    }
    bad = [k for k, (m, want) in cases.items() if len(em.guard_problems(m)) != want]
    ok = not bad and (em.MIN_TP, em.MAX_FP, em.MAX_FN) == (186, 4, 7)
    return ("guard_compares_counts", ok, f"{len(cases)} boundary checks" if ok else f"wrong: {bad}")
