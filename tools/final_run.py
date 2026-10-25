"""Final run: tests -> main.py -> evaluation -> usage report -> strict validation.

Exits non-zero if ANY of these happens:
  * a step fails (unit tests, main.py, evaluation/main.py, usage_report.py);
  * main.py prints a WARNING (including validator warnings);
  * output.csv does not contain exactly 518 data rows;
  * any COMPLETE row has no steps, no VERIFICATION step, or an uncited step;
  * strict validation of output.csv fails.
Optional: --require-llm also fails when the classifier ran in fallback mode.

This regenerates the root output.csv. Use --out PATH to write elsewhere (for a dry run).

Run: python3 tools/final_run.py
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CODE = REPO / "code"
DATASET = REPO / "dataset"
EXPECTED_ROWS = 518
PY = sys.executable


def run_step(name: str, cmd: list[str], env: dict | None = None) -> tuple[int, str]:
    print(f"[{name}] {' '.join(cmd[1:4])} ...", flush=True)
    p = subprocess.run(cmd, cwd=REPO, env={**os.environ, **(env or {})},
                       capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    status = "ok" if p.returncode == 0 else f"FAILED (exit {p.returncode})"
    print(f"[{name}] {status}", flush=True)
    if p.returncode != 0:
        print("\n".join(out.strip().splitlines()[-25:]))
    return p.returncode, out


def check_output(path: Path) -> list[str]:
    """Independent checks on the finished CSV. Returns a list of failures."""
    sys.path.insert(0, str(CODE))
    from main import validate_output_file  # noqa: E402
    failures: list[str] = []
    try:
        warnings = validate_output_file(path, DATASET, strict_complete=True)
        failures += [f"validator warning: {w}" for w in warnings]
    except Exception as e:
        failures.append(f"strict validation failed: {e}")
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) != EXPECTED_ROWS:
        failures.append(f"output.csv has {len(rows)} data rows, expected {EXPECTED_ROWS}")
    for r in rows:
        if r["deletion_status"] == "COMPLETE" and not json.loads(r["steps_json"]):
            failures.append(f"{r['case_id']}: COMPLETE row has no steps")
    return failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "output.csv"))
    ap.add_argument("--require-llm", action="store_true",
                    help="also fail when the classifier ran in fallback mode")
    args = ap.parse_args(argv)
    out = Path(args.out)
    failures: list[str] = []
    pyenv = {"PYTHONPATH": str(CODE)}

    rc, _ = run_step("1/5 unit tests", [PY, "-m", "unittest", "discover", "-s", "code/tests"], pyenv)
    if rc:
        failures.append("unit tests failed (python3 -m unittest discover -s code/tests)")
    rc, main_out = run_step("2/5 main.py", [PY, "code/main.py", "--out", str(out)])
    if rc:
        failures.append("code/main.py failed")
    warn_lines = [l.strip() for l in main_out.splitlines() if l.strip().startswith("WARNING")]
    # per-line warnings that main.py prints (validator warnings, LLM failures)
    failures += [f"main.py warning: {l}" for l in warn_lines]
    fallback = "FALLBACK mode" in main_out
    try:
        if not json.loads((CODE / ".cache" / "last_run.json").read_text(encoding="utf-8")).get("request_windows"):
            failures.append("request-window rule (state.link_requests) was not used; merge Person A's state.py")
    except Exception as e:
        failures.append(f"cannot read last_run.json: {e}")
    if fallback and args.require_llm:
        failures.append("classifier ran in fallback mode but --require-llm was given")
    rc, _ = run_step("3/5 evaluation", [PY, "code/evaluation/main.py"])
    if rc:
        failures.append("code/evaluation/main.py failed (see its output above)")
    rc, _ = run_step("4/5 usage report", [PY, "code/evaluation/usage_report.py"])
    if rc:
        failures.append("code/evaluation/usage_report.py failed")
    print("[5/5 strict validation] ...", flush=True)
    if out.exists():
        failures += check_output(out)
    else:
        failures.append(f"{out} was not written")

    # ---- one-screen summary -------------------------------------------------
    ev = {}
    try:
        ev = json.loads((CODE / ".cache" / "eval_summary.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    sys.path.insert(0, str(CODE / "evaluation"))
    usage_line = ""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("usage_report_mod", CODE / "evaluation" / "usage_report.py")
        um = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(um)
        usage_line = um.summary_line()
    except Exception as e:
        usage_line = f"(usage unavailable: {e})"
    s = ev.get("samples", {})
    print("\n" + "=" * 70)
    print("FINAL RUN SUMMARY")
    print("=" * 70)
    if out.exists():
        print(f"output.csv : {out}  sha256 {hashlib.sha256(out.read_bytes()).hexdigest()[:16]}")
    print(f"statuses   : {ev.get('full_run')}  (empty COMPLETE rows: {len(ev.get('empty_complete', []))})")
    if s:
        n = s["n"]
        print(f"samples    : status {s['status_ok']}/{n}, exact-seq {s['seq_ok']}/{n}, "
              f"P={s['precision']:.3f} R={s['recall']:.3f} F1={s['f1']:.3f}, field={s['field_acc']:.3f}")
    print(f"custom     : {ev.get('custom_passed')}/{ev.get('custom_total')} passed")
    print(f"multimodal : {ev.get('multimodal_observations')} observation(s) loaded")
    print(f"classifier : {'FALLBACK (no model calls)' if fallback else 'LLM'}")
    print(f"usage      : {usage_line}")
    print("-" * 70)
    if failures:
        print(f"FAILED: {len(failures)} problem(s)")
        for f in failures:
            print("  -", f)
        return 1
    print("PASSED: all steps ok, no warnings, 518 rows, no empty COMPLETE rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
