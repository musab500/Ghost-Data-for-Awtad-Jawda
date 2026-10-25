"""Custom case 4 (regression): a COMPLETE row must have a VERIFICATION step and
cite evidence on every step; strict validation must fail loudly on a violation."""
import csv, json, tempfile
from pathlib import Path
from _util import REPO_ROOT
from main import validate_output_file

def _write(rows):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="", encoding="utf-8")
    w = csv.writer(f); w.writerow(["case_id", "deletion_status", "steps_json"])
    for r in rows: w.writerow(r)
    f.close(); return Path(f.name)

def run():
    ds = REPO_ROOT / "dataset"
    ids = [r["case_id"] for r in csv.DictReader(open(ds / "deletion_requests.csv", newline="", encoding="utf-8"))]
    rows = [(c, "NOT_APPLICABLE", "[]") for c in ids]
    rows[0] = (ids[0], "COMPLETE", "[]")                     # empty COMPLETE
    p = _write(rows)
    try:
        validate_output_file(p, ds, strict_complete=True); strict_raised = False
    except ValueError as e:
        strict_raised = "no VERIFICATION" in str(e)
    warns = validate_output_file(p, ds, strict_complete=False)
    ok = strict_raised and len(warns) == 1 and ids[0] in warns[0]
    return ("validator_complete_rule", ok, f"strict raised={strict_raised}; warnings={len(warns)}")
