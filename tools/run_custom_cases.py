"""Run the team-made evaluation cases (code/evaluation/custom_cases/*) through
Person A's pipeline wiring (tools/sample_harness.py) and compare each with its
hand-written expected_output.csv. Exit code 1 on any mismatch.

    python3 tools/run_custom_cases.py [repo_root]
"""
import csv
import json
import os
import sys
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
HARNESS = (REPO / "tools" / "sample_harness.py").read_text().split("if __name__")[0]
CASES = REPO / "code" / "evaluation" / "custom_cases"


def run(case_dir: Path):
    os.environ["HARNESS_DATASET"] = str(case_dir)
    ns = {"__name__": "harness"}
    argv, sys.argv = sys.argv, ["harness", str(REPO)]
    try:
        exec(compile(HARNESS, "sample_harness.py", "exec"), ns)
    finally:
        sys.argv = argv
        del os.environ["HARNESS_DATASET"]
    req = next(csv.DictReader(open(case_dir / "deletion_requests.csv")))
    status, steps, _ = ns["run_case"](req)
    exp = next(csv.DictReader(open(case_dir / "expected_output.csv")))
    return status, steps, exp["deletion_status"], json.loads(exp["steps_json"])


def main():
    failed = 0
    for d in sorted(p for p in CASES.iterdir() if (p / "expected_output.csv").exists()):
        status, steps, exp_status, exp_steps = run(d)
        ok = status == exp_status and steps == exp_steps
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {d.name}: status {status} (expected {exp_status}), "
              f"{len(steps)} steps (expected {len(exp_steps)})")
        if not ok:
            for i in range(max(len(steps), len(exp_steps))):
                a = steps[i] if i < len(steps) else None
                b = exp_steps[i] if i < len(exp_steps) else None
                if a != b:
                    print(f"      step {i + 1}: got {a}\n               expected {b}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
