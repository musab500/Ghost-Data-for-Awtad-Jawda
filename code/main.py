"""End-to-end deletion-audit pipeline (Person B).

Wires Person A's parsers / state / rules together with identity resolution,
request classification and step assembly, exactly as specified in
``code/parsers/NOTES.md`` (Integration contract), and writes the submission
``output.csv`` at the repository root.

Run:
    python3 code/main.py                 # writes ./output.csv for all 518 cases
    python3 code/main.py --out out.csv   # custom output path
    python3 code/main.py --validate-only ./output.csv

The pipeline is deterministic. A model is used only by the request classifier,
and only when an API key is present (see classifier.py); the final run in this
environment used the deterministic fallback classifier.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# Make sibling modules importable whether run as `code/main.py` or `-m main`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from models import norm_email  # noqa: E402
from parsers import load_evidence  # noqa: E402
from parsers.activity import parse_activity, non_deletion_flags  # noqa: E402
from state import EvidenceIndex, CaseWindow, flagged_cases  # noqa: E402
from rules import load_rules, decide, OPERATION_ORDER, OP_STATE  # noqa: E402
from identity import IdentityResolver  # noqa: E402
from classifier import RequestClassifier  # noqa: E402
from steps import status_for, assemble_steps  # noqa: E402

VALID_STATUS = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
STEP_FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation",
               "state", "evidence_id"]


def _case_key(case_id: str):
    """Natural sort key for case ids (case_33 < case_100)."""
    try:
        return (0, int(case_id.split("_", 1)[1]))
    except (IndexError, ValueError):
        return (1, case_id)


def _load_multimodal(skipped, dataset_dir):
    """Optional hook for Person A's multimodal.py (PDF/PNG -> Observations).

    Degrades to [] when the module and cache are both absent, so the
    deterministic pipeline always runs. Accepted shapes:
      * a module ``multimodal`` exposing multimodal_observations/observations/load
      * a JSON cache at code/.cache/multimodal_observations.json listing
        Observation kwargs (identifiers as a list).
    """
    # 1) module
    try:
        import multimodal  # type: ignore
        for name in ("multimodal_observations", "observations", "load"):
            fn = getattr(multimodal, name, None)
            if callable(fn):
                for args in ((skipped, dataset_dir), (skipped,), ()):
                    try:
                        out = fn(*args)
                        if out:
                            return list(out)
                    except TypeError:
                        continue
                    except Exception:
                        break
    except Exception:
        pass
    # 2) JSON cache
    try:
        from models import Observation
        cache = Path(__file__).resolve().parent / ".cache" / "multimodal_observations.json"
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            obs = []
            for d in data:
                d = dict(d)
                d["identifiers"] = set(d.get("identifiers", []))
                obs.append(Observation(**d))
            return obs
    except Exception:
        pass
    return []


class Pipeline:
    """Builds the shared evidence index once, then predicts every case.

    Workflow assignment is global (the activity stream is shared across cases),
    so it is computed once over *all* requests — the 32 samples and the 518
    evaluation requests together — in line with the integration contract.
    """

    def __init__(self, dataset_dir: str | Path, use_llm: bool | None = None):
        self.ds = Path(dataset_dir)
        obs, scopes, skipped = load_evidence(self.ds)
        obs = obs + _load_multimodal(skipped, self.ds)
        self.skipped = skipped
        events, aobs, links = parse_activity(self.ds)
        self.events = events
        self.idx = EvidenceIndex(obs + aobs, scopes, links, events)
        self.rules = load_rules(self.ds)
        self.resolver = IdentityResolver(self.ds)
        self.classifier = RequestClassifier(use_llm=use_llm)

        self.requests = self._load_requests()              # case_id -> row
        self.identity = {}                                 # case_id -> Identity|None
        self.direct = {}                                   # case_id -> {rid: [reasons]}
        for c, r in self.requests.items():
            ident = self.resolver.resolve(r["email"])
            self.identity[c] = ident
            self.direct[c] = {k: list(v) for k, v in self.idx.direct_links(ident).items()} if ident else {}

        self.workflow = self._assign_workflow()            # case_id -> {rid: reason}

    # -- setup -------------------------------------------------------------
    def _load_requests(self) -> dict:
        rows = {}
        for name in ("sample_deletion_requests.csv", "deletion_requests.csv"):
            path = self.ds / name
            if not path.exists():
                continue
            with open(path, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    rows[r["case_id"]] = r
        return rows

    def _expects_complete(self, case_id: str, audit_at: str) -> bool:
        decs = [decide(self.idx.state_at(r, audit_at), self.rules, audit_at)
                for r in self.direct[case_id]]
        return not any(d and d.violates for d in decs)

    def _assign_workflow(self) -> dict:
        windows = []
        for c, r in self.requests.items():
            audit = r["audit_at"]
            windows.append(CaseWindow(
                c, r["reported_at"], audit,
                {self.idx.category_of(x) for x in self.direct[c]},
                r["reported_status"],
                self._expects_complete(c, audit),
                set(self.direct[c]),
            ))
        return self.idx.assign_workflow(windows)

    # -- prediction --------------------------------------------------------
    def predict(self, case_id: str):
        r = self.requests[case_id]
        if not self.classifier.is_deletion(r["request_text"]):
            return "NOT_APPLICABLE", []
        if self.identity[case_id] is None:
            # Email resolved to no known identity: no records can be proven.
            return "INCOMPLETE", []
        audit = r["audit_at"]
        linked = {rid: list(reasons) for rid, reasons in self.direct[case_id].items()}
        for rid, why in self.workflow.get(case_id, {}).items():
            linked.setdefault(rid, []).append(why)
        decisions = []
        for rid in sorted(linked):
            d = decide(self.idx.state_at(rid, audit), self.rules, audit)
            if d:
                decisions.append(d)
        status = status_for(decisions)
        return status, assemble_steps(decisions, status)

    def predict_all(self, case_ids):
        return {c: self.predict(c) for c in case_ids}

    def classifier_crosscheck(self):
        """Compare classifier NOT_APPLICABLE decisions with the CLASSIFY_REQUEST
        workflow flags (claims only). Returns (classifier_na, flagged, agree)."""
        flags = non_deletion_flags(self.events)
        fc = flagged_cases(self.direct, flags)  # case_id -> activity_id
        flagged = set(fc)
        classifier_na = {c for c, r in self.requests.items()
                         if c in self.direct and not self.classifier.is_deletion(r["request_text"])}
        # Restrict to the evaluation set the flags were derived on.
        full = set(self._full_case_ids())
        return classifier_na & full, flagged & full, (classifier_na & flagged & full)

    def _full_case_ids(self):
        path = self.ds / "deletion_requests.csv"
        with open(path, newline="", encoding="utf-8") as fh:
            return [r["case_id"] for r in csv.DictReader(fh)]


# --------------------------------------------------------------------------- #
#  Output + validation
# --------------------------------------------------------------------------- #
def write_output(pipeline: Pipeline, out_path: Path) -> list[tuple[str, str, list]]:
    case_ids = sorted(pipeline._full_case_ids(), key=_case_key)
    rows = []
    for c in case_ids:
        status, steps = pipeline.predict(c)
        rows.append((c, status, steps))
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for c, status, steps in rows:
            w.writerow([c, status, json.dumps(steps, separators=(",", ":"), ensure_ascii=True)])
    return rows


def validate_output_file(out_path: Path, dataset_dir: Path) -> None:
    """Structural validation independent of the generator. Raises on any
    violation of the output contract (fails loudly)."""
    ds = Path(dataset_dir)
    expected = [r["case_id"] for r in csv.DictReader(
        open(ds / "deletion_requests.csv", newline="", encoding="utf-8"))]

    with open(out_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        if header != ["case_id", "deletion_status", "steps_json"]:
            raise ValueError(f"bad header: {header!r}")
        data = list(reader)

    if len(data) != len(expected):
        raise ValueError(f"expected {len(expected)} rows, got {len(data)}")
    seen = [row[0] for row in data]
    if set(seen) != set(expected):
        missing = set(expected) - set(seen)
        extra = set(seen) - set(expected)
        raise ValueError(f"case_id mismatch; missing={sorted(missing)} extra={sorted(extra)}")
    if len(set(seen)) != len(seen):
        raise ValueError("duplicate case_id rows")
    if seen != sorted(seen, key=_case_key):
        raise ValueError("rows are not sorted by case_id")

    for case_id, status, steps_json in data:
        if status not in VALID_STATUS:
            raise ValueError(f"{case_id}: bad status {status!r}")
        try:
            steps = json.loads(steps_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"{case_id}: steps_json is not valid JSON: {e}") from e
        if not isinstance(steps, list):
            raise ValueError(f"{case_id}: steps_json must be an array")
        if status == "NOT_APPLICABLE" and steps != []:
            raise ValueError(f"{case_id}: NOT_APPLICABLE must have []")

        prev_phase = -1
        prev_within = None
        for i, s in enumerate(steps, 1):
            if set(s) != set(STEP_FIELDS):
                raise ValueError(f"{case_id} step {i}: fields {sorted(s)} != {STEP_FIELDS}")
            if s["step_no"] != i:
                raise ValueError(f"{case_id} step {i}: step_no={s['step_no']} not consecutive")
            if s["phase"] not in PHASE_ORDER:
                raise ValueError(f"{case_id} step {i}: bad phase {s['phase']!r}")
            if s["operation"] not in OP_STATE:
                raise ValueError(f"{case_id} step {i}: bad operation {s['operation']!r}")
            if s["state"] != OP_STATE[s["operation"]]:
                raise ValueError(f"{case_id} step {i}: state {s['state']!r} != {OP_STATE[s['operation']]!r}")
            ph = PHASE_ORDER[s["phase"]]
            if ph < prev_phase:
                raise ValueError(f"{case_id} step {i}: phase out of order")
            within = (OPERATION_ORDER.index(s["operation"]), s["system_id"], s["resource_id"])
            if ph != prev_phase:
                prev_within = None
            if prev_within is not None and within < prev_within:
                raise ValueError(f"{case_id} step {i}: operation/system/resource out of order")
            prev_phase, prev_within = ph, within
            # Evidence citation contract.
            if status == "COMPLETE":
                if s["phase"] != "VERIFICATION":
                    raise ValueError(f"{case_id}: COMPLETE case has non-verification step")
                if not s["evidence_id"]:
                    raise ValueError(f"{case_id} step {i}: COMPLETE verification must cite evidence")
            if s["phase"] == "FINDING" and not s["evidence_id"]:
                raise ValueError(f"{case_id} step {i}: FINDING must cite evidence")
            if status == "INCOMPLETE" and s["phase"] in ("REMEDIATION", "VERIFICATION") and s["evidence_id"]:
                raise ValueError(f"{case_id} step {i}: INCOMPLETE {s['phase']} must have empty evidence_id")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ghost Data deletion-audit pipeline")
    repo_root = Path(__file__).resolve().parents[1]
    ap.add_argument("--dataset", default=str(repo_root / "dataset"))
    ap.add_argument("--out", default=str(repo_root / "output.csv"))
    ap.add_argument("--validate-only", metavar="CSV", default=None,
                    help="validate an existing output.csv and exit")
    args = ap.parse_args(argv)

    ds = Path(args.dataset)
    if args.validate_only:
        validate_output_file(Path(args.validate_only), ds)
        print(f"OK: {args.validate_only} is valid")
        return 0

    pipeline = Pipeline(ds)
    out = Path(args.out)
    rows = write_output(pipeline, out)
    validate_output_file(out, ds)

    from collections import Counter
    dist = Counter(status for _, status, _ in rows)
    na, flagged, agree = pipeline.classifier_crosscheck()
    print(f"wrote {len(rows)} rows -> {out}")
    print(f"status distribution: {dict(dist)}")
    print(f"classifier NOT_APPLICABLE: {len(na)}  |  CLASSIFY_REQUEST flags: {len(flagged)}  |  agreement: {len(agree)}")
    print("validation: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
