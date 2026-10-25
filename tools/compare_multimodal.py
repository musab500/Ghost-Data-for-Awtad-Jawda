"""Compare the vision-model (VLM) transcription of each screenshot with the manual
transcription (not part of the prediction path).

    python3 tools/compare_multimodal.py              # compare cached results; no API calls
    python3 tools/compare_multimodal.py --run        # first call the VLM once per PNG (needs
                                                     #   ANTHROPIC_API_KEY, e.g. in .env), then compare
    python3 tools/compare_multimodal.py --freeze     # copy the API results into code/multimodal_vlm/
                                                     #   (committed, so code.zip reproduces without a key)
    python3 tools/compare_multimodal.py --pipeline-diff   # run code/main.py with manual transcriptions
                                                     #   allowed vs forbidden and list the changed rows
    python3 tools/compare_multimodal.py --out report.md

Per file it reports: records matched (same resource), missing (manual only),
extra (VLM only), and field mismatches on matched records. A file with no VLM
result is reported as such; nothing is ever filled in.
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "code"))

import multimodal  # noqa: E402
from models import CATEGORIES, STATES, normalize_identifier  # noqa: E402
from parsers import load_evidence  # noqa: E402
from parsers.activity import parse_activity, resource_aliases  # noqa: E402

FIELDS = ("system_id", "category", "state", "identifiers", "observed_at")
# Fields that decide a step in the pipeline; observed_at only orders observations.
PIPELINE_FIELDS = ("system_id", "category", "state", "identifiers")


def _norm_record(r: dict, aliases: dict) -> tuple[str | None, dict | None, str | None]:
    """(resource_id, normalised fields, drop reason) for one transcribed record,
    using the same resolution and validation as the pipeline."""
    rid, system = multimodal._resolve(
        ([r["resource_id"]] if r.get("resource_id") else []) + list(r.get("keys") or []), aliases)
    state = str(r.get("state", "")).upper()
    cat = str(r.get("category", "")).upper() or None
    if rid is None:
        return None, None, f"no resource id or alias in {r.get('keys')}"
    if state not in STATES:
        return rid, None, f"invalid state {state!r}"
    if cat is not None and cat not in CATEGORIES:
        return rid, None, f"invalid category {cat!r}"
    return rid, {
        "system_id": system or r.get("system_id") or "",
        "category": cat,
        "state": state,
        "identifiers": frozenset(n for n in (normalize_identifier(x) for x in r.get("identifiers") or []) if n),
        "observed_at": r.get("observed_at") or "",
    }, None


def compare_records(manual: list[dict], vlm: list[dict], aliases: dict) -> dict:
    """Compare two record lists for one file."""
    def index(records):
        by, dropped = {}, []
        for r in records:
            rid, f, why = _norm_record(r, aliases)
            if f is None:
                dropped.append(why)
            else:
                by[rid] = f
        return by, dropped

    m, m_drop = index(manual)
    v, v_drop = index(vlm)
    matched = sorted(set(m) & set(v))
    mismatches = []
    for rid in matched:
        for fld in FIELDS:
            if m[rid][fld] != v[rid][fld]:
                mismatches.append({"resource_id": rid, "field": fld,
                                   "manual": _show(m[rid][fld]), "vlm": _show(v[rid][fld])})
    missing, extra = sorted(set(m) - set(v)), sorted(set(v) - set(m))
    pipeline_identical = (not missing and not extra and
                          not any(x["field"] in PIPELINE_FIELDS for x in mismatches))
    return {"manual_n": len(m), "vlm_n": len(v), "matched": matched, "missing": missing,
            "extra": extra, "mismatches": mismatches, "manual_dropped": m_drop,
            "vlm_dropped": v_drop, "pipeline_identical": pipeline_identical}


def _show(v):
    return sorted(v) if isinstance(v, frozenset) else v


def _vlm_entry(ev, cache_dir: Path, frozen_dir: Path):
    """The API result for this file (never a manual one), or None."""
    name = f"{multimodal._cache_key(ev.path, multimodal._model())}.json"
    for d in (cache_dir, frozen_dir):
        p = d / name
        if p.exists():
            e = json.loads(p.read_text(encoding="utf-8"))
            if e.get("extractor", {}).get("method") == "api":
                return e
    return None


def _manual_entry(ev, manual_dir: Path):
    sha = multimodal._sha256(ev.path)
    for p in sorted(manual_dir.glob(f"{sha[:16]}__*.json")):
        e = json.loads(p.read_text(encoding="utf-8"))
        if e.get("file_sha256") == sha:
            return e
    return None


def build_report(dataset: Path, cache_dir: Path, frozen_dir: Path, manual_dir: Path) -> tuple[str, dict]:
    _o, _s, skipped = load_evidence(dataset)
    aliases = resource_aliases(parse_activity(dataset)[0])
    pngs = sorted((e for e in skipped if e.kind == "png"), key=lambda e: e.file_id)
    rows, details, tot = [], [], dict(files=0, compared=0, manual=0, vlm=0, matched=0, missing=0,
                                      extra=0, mism=0, identical=0)
    for ev in pngs:
        tot["files"] += 1
        man, vlm = _manual_entry(ev, manual_dir), _vlm_entry(ev, cache_dir, frozen_dir)
        name = Path(ev.rel_path).name
        if man is None or vlm is None:
            what = "no VLM result" if vlm is None else "no manual transcription"
            rows.append(f"| {ev.file_id} | {name} | - | - | - | - | - | - | {what} |")
            continue
        c = compare_records(man["records"], vlm["records"], aliases)
        tot["compared"] += 1
        tot["manual"] += c["manual_n"]; tot["vlm"] += c["vlm_n"]; tot["matched"] += len(c["matched"])
        tot["missing"] += len(c["missing"]); tot["extra"] += len(c["extra"])
        tot["mism"] += len(c["mismatches"]); tot["identical"] += c["pipeline_identical"]
        rows.append(f"| {ev.file_id} | {name} | {c['manual_n']} | {c['vlm_n']} | {len(c['matched'])} | "
                    f"{len(c['missing'])} | {len(c['extra'])} | {len(c['mismatches'])} | "
                    f"{'yes' if c['pipeline_identical'] else 'NO'} |")
        for rid in c["missing"]:
            details.append(f"- {ev.file_id} **missing** in VLM output: `{rid}`")
        for rid in c["extra"]:
            details.append(f"- {ev.file_id} **extra** in VLM output: `{rid}`")
        for x in c["mismatches"]:
            details.append(f"- {ev.file_id} `{x['resource_id']}` field `{x['field']}`: "
                           f"manual={x['manual']} vs VLM={x['vlm']}")
        for side, drops in (("manual", c["manual_dropped"]), ("VLM", c["vlm_dropped"])):
            for why in drops:
                details.append(f"- {ev.file_id} {side} record dropped by validation: {why}")
    head = ("| File | Name | Manual recs | VLM recs | Matched | Missing | Extra | Field mismatches | "
            "Pipeline-identical |\n|---|---|---|---|---|---|---|---|---|")
    lines = [head, *rows]
    if tot["compared"]:
        lines.append(f"| **total** | {tot['compared']}/{tot['files']} files compared | {tot['manual']} | {tot['vlm']} | "
                     f"{tot['matched']} | {tot['missing']} | {tot['extra']} | {tot['mism']} | "
                     f"{tot['identical']}/{tot['compared']} |")
    else:
        lines.append(f"| **total** | 0/{tot['files']} files compared: no VLM result exists yet | | | | | | | |")
    text = "\n".join(lines)
    if details:
        text += "\n\nDifferences:\n\n" + "\n".join(details)
    return text, tot


def freeze(dataset: Path, cache_dir: Path, frozen_dir: Path) -> int:
    _o, _s, skipped = load_evidence(dataset)
    frozen_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for ev in skipped:
        if ev.kind != "png":
            continue
        name = f"{multimodal._cache_key(ev.path, multimodal._model())}.json"
        src = cache_dir / name
        if src.exists() and _vlm_entry(ev, cache_dir, Path("/nonexistent")):
            shutil.copy(src, frozen_dir / name)
            n += 1
    return n


def pipeline_diff(dataset: Path) -> str:
    """Run code/main.py with manual transcriptions allowed vs forbidden."""
    outs = {}
    with tempfile.TemporaryDirectory() as d:
        for label, allow in (("manual-allowed", "1"), ("vlm-only", "0")):
            out = Path(d) / f"{label}.csv"
            env = {**os.environ, "MULTIMODAL_ALLOW_MANUAL": allow}
            r = subprocess.run([sys.executable, str(REPO / "code" / "main.py"), "--dataset", str(dataset),
                                "--out", str(out)], env=env, capture_output=True, text=True)
            if r.returncode != 0:
                return f"main.py failed ({label}): {r.stderr[-300:]}"
            outs[label] = {x["case_id"]: x for x in csv.DictReader(open(out))}
    a, b = outs["manual-allowed"], outs["vlm-only"]
    changed = [c for c in sorted(a) if a[c] != b.get(c)]
    msg = f"rows: {len(a)} vs {len(b)}; changed rows: {len(changed)}"
    for c in changed:
        sa = {(s["phase"], s["resource_id"], s["operation"]) for s in json.loads(a[c]["steps_json"])}
        sb = {(s["phase"], s["resource_id"], s["operation"]) for s in json.loads(b[c]["steps_json"])}
        msg += (f"\n  {c}: {a[c]['deletion_status']} -> {b[c]['deletion_status']}; "
                f"lost {sorted(sa - sb)}; gained {sorted(sb - sa)}")
    return msg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=str(REPO / "dataset"))
    ap.add_argument("--run", action="store_true", help="call the VLM once per PNG first (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--freeze", action="store_true", help="copy API results into code/multimodal_vlm/")
    ap.add_argument("--pipeline-diff", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    ds = Path(a.dataset)
    multimodal.load_dotenv()
    if a.run:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("--run needs ANTHROPIC_API_KEY (environment or .env); nothing was called.")
        os.environ["MULTIMODAL_ALLOW_MANUAL"] = "0"      # only API results may satisfy a PNG
        _o, _s, skipped = load_evidence(ds)
        aliases = resource_aliases(parse_activity(ds)[0])
        multimodal.multimodal_observations([e for e in skipped if e.kind == "png"], aliases)
        print(f"usage written to {multimodal.USAGE_PATH}\n"
              + multimodal.USAGE_PATH.read_text(encoding="utf-8")[:600], file=sys.stderr)
    if a.freeze:
        print(f"froze {freeze(ds, multimodal.CACHE_DIR, multimodal.FROZEN_DIR)} API result(s) into "
              f"{multimodal.FROZEN_DIR}", file=sys.stderr)
    text, _ = build_report(ds, multimodal.CACHE_DIR, multimodal.FROZEN_DIR, multimodal.MANUAL_DIR)
    if a.pipeline_diff:
        text += "\n\nPipeline output, manual transcriptions allowed vs VLM only (code/main.py):\n\n" + pipeline_diff(ds)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
