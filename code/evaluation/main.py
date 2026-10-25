"""Evaluation harness (Person B).

Runs the full pipeline on the public sample requests, compares predictions with
``dataset/sample_output.csv`` and WRITES ``evaluation_report.md`` from
``report_template.md``: the narrative lives in the template, every number is
computed here from the current run, so the report cannot go stale.

Computed and written into the report:
  * status accuracy, exact-sequence accuracy, step precision / recall / F1,
    field-level accuracy, per-case diffs (with documented causes);
  * custom-case results (``custom_cases/``);
  * full 518-request status counts and average steps;
  * classifier mode and agreement with the CLASSIFY_REQUEST flags;
  * multimodal files used and their extraction method;
  * recorded token usage and cost (from usage_report.collect()).

Pass thresholds exist ONLY in this file (MIN_* below), never in the prose.

Run:  python3 code/evaluation/main.py            # print + write the report
      python3 code/evaluation/main.py --no-write # print only
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_DIR.parent
EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

from main import Pipeline  # noqa: E402

STEP_KEYS = ("phase", "system_id", "resource_id", "operation", "evidence_id")
FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")

# Regression guard (defined ONLY here). Step precision/recall are compared on the underlying
# counts, never on rounded floats: P = tp/(tp+fp) and R = tp/(tp+fn).
# The baseline below is 186/190 precision and 186/193 recall.
MIN_STATUS_OK = 32
MIN_EXACT_SEQ = 25
MIN_TP = 186
MAX_FP = 4
MAX_FN = 7

REPORT_PATH = EVAL_DIR / "evaluation_report.md"
TEMPLATE_PATH = EVAL_DIR / "report_template.md"
CAUSES_PATH = EVAL_DIR / "known_causes.json"
COMPARISON_PATH = EVAL_DIR / "classifier_comparison.md"
SUMMARY_PATH = CODE_DIR / ".cache" / "eval_summary.json"


def _key(step: dict) -> tuple:
    return tuple(step[k] for k in STEP_KEYS)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod


# --------------------------------------------------------------------------- #
#  Computation
# --------------------------------------------------------------------------- #
def evaluate_samples(pipeline: Pipeline, dataset_dir: Path) -> dict:
    ds = Path(dataset_dir)
    reqs = list(csv.DictReader(open(ds / "sample_deletion_requests.csv", newline="", encoding="utf-8")))
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
        for a, b in zip(steps, gold_steps):
            for f in FIELDS:
                field_total += 1
                field_hits += int(a.get(f) == b.get(f))
        field_total += abs(len(steps) - len(gold_steps)) * len(FIELDS)
        if P != G:
            diffs.append({"case": c, "pred": status, "gold": g["deletion_status"],
                          "missing": sorted(gs - ps), "extra": sorted(ps - gs),
                          "order_only": ps == gs})
    n = len(reqs)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"n": n, "status_ok": status_ok, "seq_ok": seq_ok, "precision": precision,
            "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn,
            "field_acc": field_hits / field_total if field_total else 1.0,
            "field_hits": field_hits, "field_total": field_total, "diffs": diffs}


def full_run_stats(pipeline: Pipeline) -> dict:
    ids = pipeline._full_case_ids()
    by_status = defaultdict(list)
    for c in ids:
        status, steps = pipeline.predict(c)
        by_status[status].append(len(steps))
    empty_complete = [c for c in ids
                      if (lambda s: s[0] == "COMPLETE" and not s[1])(pipeline.predict(c))]
    return {"total": len(ids),
            "rows": {s: {"count": len(v), "avg_steps": (sum(v) / len(v)) if v else 0.0,
                         "total_steps": sum(v)} for s, v in sorted(by_status.items())},
            "empty_complete": empty_complete}


def run_custom_cases() -> list[tuple]:
    results = []
    cdir = EVAL_DIR / "custom_cases"
    if not cdir.is_dir():
        return results
    if str(cdir) not in sys.path:
        sys.path.insert(0, str(cdir))
    for path in sorted(cdir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            mod = _load_module(path, f"custom_{path.stem}")
        except Exception as e:  # a broken case is a failure, reported loudly
            results.append((path.stem, False, f"import error: {e}"))
            continue
        if hasattr(mod, "run"):
            try:
                results.append(tuple(mod.run()))
            except Exception as e:
                results.append((path.stem, False, f"run() error: {e}"))
        if hasattr(mod, "CASES"):
            results.extend(tuple(row) for row in mod.CASES)
    return results


def multimodal_rows(pipeline: Pipeline) -> list[dict]:
    """One row per multimodal evidence file: kind, extraction method, observations."""
    groups = defaultdict(lambda: {"obs": 0, "resources": set()})
    for o in getattr(pipeline, "multimodal_obs", []):
        raw = o.raw or {}
        method = raw.get("method") or raw.get("extraction") or raw.get("source_method") or "unspecified"
        g = groups[(o.evidence_id, o.source_kind, method)]
        g["obs"] += 1
        g["resources"].add(o.resource_id)
    return [{"evidence_id": k[0], "kind": k[1], "method": k[2], "observations": v["obs"],
             "resources": sorted(v["resources"])} for k, v in sorted(groups.items())]


def guard_problems(m: dict) -> list[str]:
    """Regression guard on the sample results; returns a list of violations (empty = pass)."""
    n, problems = m["n"], []
    if m["status_ok"] < MIN_STATUS_OK:
        problems.append(f"status accuracy {m['status_ok']}/{n} below minimum {MIN_STATUS_OK}")
    if m["seq_ok"] < MIN_EXACT_SEQ:
        problems.append(f"exact-sequence {m['seq_ok']}/{n} below minimum {MIN_EXACT_SEQ}")
    if m["tp"] < MIN_TP:
        problems.append(f"matched steps tp={m['tp']} below minimum {MIN_TP}")
    if m["fp"] > MAX_FP:
        problems.append(f"extra steps fp={m['fp']} above maximum {MAX_FP} (precision {m['precision']:.5f})")
    if m["fn"] > MAX_FN:
        problems.append(f"missing steps fn={m['fn']} above maximum {MAX_FN} (recall {m['recall']:.5f})")
    return problems


# --------------------------------------------------------------------------- #
#  Rendering
# --------------------------------------------------------------------------- #
def _table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def render_values(m: dict, full: dict, custom: list, mm: list, pipeline: Pipeline, usage_line: str) -> dict:
    causes = {}
    try:
        causes = json.loads(CAUSES_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    n = m["n"]
    sample_metrics = _table(["Metric", "Result"], [
        ["Status accuracy", f"{m['status_ok']} / {n} ({m['status_ok'] / n:.3f})"],
        ["Exact step-sequence match", f"{m['seq_ok']} / {n} ({m['seq_ok'] / n:.3f})"],
        ["Step precision", f"{m['precision']:.3f} (tp={m['tp']}, fp={m['fp']})"],
        ["Step recall", f"{m['recall']:.3f} (fn={m['fn']})"],
        ["Step F1", f"{m['f1']:.3f}"],
        ["Field-level accuracy", f"{m['field_acc']:.3f} ({m['field_hits']} / {m['field_total']} fields)"],
    ])
    if m["diffs"]:
        diff_summary = (f"{len(m['diffs'])} of {n} sample cases differ from the gold sequence; "
                        f"{sum(1 for d in m['diffs'] if d['pred'] != d['gold'])} of them also have a wrong status.\n\n"
                        + _table(["Case", "Pred", "Gold", "Missing", "Extra", "Documented cause"], [
                            [d["case"], d["pred"], d["gold"], len(d["missing"]), len(d["extra"]),
                             causes.get(d["case"], "**no documented cause: investigate**")]
                            for d in m["diffs"]]))
        blocks = []
        for d in m["diffs"]:
            blocks.append(f"{d['case']} (pred={d['pred']}, gold={d['gold']})")
            blocks += [f"  MISSING {s}" for s in d["missing"]] + [f"  EXTRA   {s}" for s in d["extra"]]
            if d["order_only"]:
                blocks.append("  (same steps, different order)")
        diff_details = "```text\n" + "\n".join(blocks) + "\n```"
    else:
        diff_summary = f"All {n} sample cases match the gold sequence exactly."
        diff_details = "(none)"

    total = full["total"]
    full_run = _table(["deletion_status", "rows", "avg steps / row", "total steps"],
                      [[s, v["count"], f"{v['avg_steps']:.2f}", v["total_steps"]]
                       for s, v in full["rows"].items()]
                      + [["**total**", total, "", sum(v["total_steps"] for v in full["rows"].values())]])
    full_run += (f"\n\nCOMPLETE rows with no steps: {len(full['empty_complete'])}"
                 + (f" ({', '.join(full['empty_complete'])})" if full["empty_complete"] else ""))

    multimodal = (_table(["Evidence file", "Kind", "Extraction method", "Observations"],
                         [[r["evidence_id"], r["kind"], r["method"], r["observations"]] for r in mm])
                  if mm else "No multimodal observations were loaded in this run "
                             "(`multimodal.py` and its cache were not available).")
    na, flagged, agree = pipeline.classifier_crosscheck()
    clf = pipeline.classifier
    sample_na = sorted(c for c in pipeline.requests
                       if c not in set(pipeline._full_case_ids()) and not clf.is_deletion(pipeline.requests[c]["request_text"]))
    classifier_crosscheck = _table(["Item", "Value"], [
        ["Classifier mode in this run", (f"LLM: `{clf.provider}` / `{clf.model}`" if clf.use_llm else "deterministic fallback (no model)")],
        ["Classifier calls this run / cache hits", f"{clf.calls} / {clf.cache_hits}"],
        ["Classifier NOT_APPLICABLE (full set)", len(na)],
        ["CLASSIFY_REQUEST workflow flags (full set)", len(flagged)],
        ["Agreement", f"{len(agree)} / {len(flagged)}"],
        ["Disagreements", sorted((na | flagged) - agree) or "none"],
        ["Sample requests classified NOT_APPLICABLE", sample_na or "none"],
    ])
    comparison = (COMPARISON_PATH.read_text(encoding="utf-8").strip() if COMPARISON_PATH.exists()
                  else "Not run. `python3 code/evaluation/classifier_eval.py` needs an `ANTHROPIC_API_KEY`; "
                       "it writes the LLM-only, fallback-only and combined results to "
                       "`code/evaluation/classifier_comparison.md`, which this report then includes.")
    custom_md = (_table(["Case", "Result", "Detail"],
                        [[name, "PASS" if ok else "FAIL", detail] for name, ok, detail in custom])
                 + f"\n\n{sum(1 for _, ok, _ in custom if ok)} / {len(custom)} passed.") if custom else "(no custom cases found)"
    return {"sample_metrics": sample_metrics, "diff_summary": diff_summary, "diff_details": diff_details,
            "full_run": full_run, "multimodal": multimodal, "classifier_crosscheck": classifier_crosscheck,
            "classifier_comparison": comparison, "custom_cases": custom_md, "usage_summary": usage_line}


def render_report(values: dict) -> str:
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    if "{{" in text:
        left = sorted({t.split("}}")[0] for t in text.split("{{")[1:]})
        raise SystemExit(f"ERROR: unresolved placeholder(s) in report template: {left}")
    return text


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true", help="print results but do not write the report")
    args = ap.parse_args(argv)

    ds = REPO_ROOT / "dataset"
    pipeline = Pipeline(ds)
    m = evaluate_samples(pipeline, ds)
    full = full_run_stats(pipeline)
    custom = run_custom_cases()
    mm = multimodal_rows(pipeline)
    usage = _load_module(EVAL_DIR / "usage_report.py", "usage_report_mod")
    usage_line = usage.summary_line()

    n = m["n"]
    print(f"status {m['status_ok']}/{n} | exact-seq {m['seq_ok']}/{n} | "
          f"P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f} | field={m['field_acc']:.3f}")
    print("full run:", {s: v["count"] for s, v in full["rows"].items()},
          "| empty COMPLETE rows:", len(full["empty_complete"]))
    print(f"custom cases: {sum(1 for _, ok, _ in custom if ok)}/{len(custom)} passed; "
          f"multimodal observations: {len(getattr(pipeline, 'multimodal_obs', []))}")
    for name, ok, detail in custom:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    problems = guard_problems(m)
    failed = [name for name, ok, _ in custom if not ok]
    if failed:
        problems.append(f"custom case(s) failed: {failed}")

    summary = {"samples": {k: m[k] for k in ("n", "status_ok", "seq_ok", "precision", "recall", "f1", "field_acc")},
               "full_run": {s: v["count"] for s, v in full["rows"].items()},
               "empty_complete": full["empty_complete"],
               "custom_passed": sum(1 for _, ok, _ in custom if ok), "custom_total": len(custom),
               "multimodal_observations": len(getattr(pipeline, "multimodal_obs", [])),
               "classifier_mode": "llm" if pipeline.classifier.use_llm else "fallback",
               "problems": problems}
    SUMMARY_PATH.parent.mkdir(exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=1), encoding="utf-8")

    if not args.no_write:
        REPORT_PATH.write_text(render_report(render_values(m, full, custom, mm, pipeline, usage_line)),
                               encoding="utf-8")
        print(f"wrote {REPORT_PATH}")
    if problems:
        print("EVALUATION FAILED:", *problems, sep="\n  - ")
        return 1
    print("guard: OK (sample results at or above the minimums in code/evaluation/main.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
