"""Generate code/evaluation/vlm_section.md: the vision-model (VLM) section of the delivery reports.

Every number comes from recorded files; the only hand-written part is the review of each
file against its image (code/evaluation/vlm_review.json). No API calls.

Inputs
  code/multimodal_usage_prior_runs.json   calls of the earlier runs (labelled run1 ...)
  code/.cache/multimodal_usage.json       ledger of the latest run, if present
  code/evaluation/vlm_runs/<model>_<prompt>/<file_id>.json   raw results of each run
  code/multimodal_manual/                 the committed manual transcriptions
  code/evaluation/vlm_review.json         verdict per file and why

Run: python3 tools/vlm_section.py
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "code"))
sys.path.insert(0, str(REPO / "tools"))

import compare_multimodal as cmp  # noqa: E402
import multimodal  # noqa: E402
from parsers import load_evidence  # noqa: E402
from parsers.activity import parse_activity, resource_aliases  # noqa: E402

EVAL = REPO / "code" / "evaluation"
OUT = EVAL / "vlm_section.md"


def _label(call: dict) -> str:
    m = re.match(r"(run\d+|diagnostic)", str(call.get("run", "")))
    return m.group(1) if m else "latest"


def runs_table(review: dict) -> list[str]:
    prior = json.loads(multimodal.PRIOR_RUNS_PATH.read_text(encoding="utf-8")).get("calls", [])
    seen = {(c.get("file_id"), c.get("at"), c.get("model"), c.get("input_tokens")) for c in prior}
    live = []
    try:
        live = json.loads(multimodal.USAGE_PATH.read_text(encoding="utf-8")).get("calls", [])
    except (OSError, ValueError):
        pass
    fresh = [dict(c, run="latest") for c in live
             if (c.get("file_id"), c.get("at"), c.get("model"), c.get("input_tokens")) not in seen]
    by = defaultdict(list)
    for c in prior + fresh:
        by[_label(c)].append(c)
    rows = ["| Run | Model | Prompt | API responses | OK | Failed | Input tokens | Output tokens | Notes |",
            "|---|---|---|---|---|---|---|---|---|"]
    tot = [0, 0, 0, 0, 0]
    for name in sorted(by, key=lambda n: (n == "latest", n)):
        cs = by[name]
        meta = review["runs"].get(name, {})
        models = sorted({c.get("model", "") for c in cs})
        ok = sum(1 for c in cs if c.get("ok"))
        tin, tout = sum(c.get("input_tokens", 0) for c in cs), sum(c.get("output_tokens", 0) for c in cs)
        rows.append(f"| {name} | {', '.join(models)} | {meta.get('prompt', '')} | {len(cs)} | {ok} | {len(cs) - ok} | "
                    f"{tin:,} | {tout:,} | {meta.get('note', '')} |")
        for i, v in enumerate((len(cs), ok, len(cs) - ok, tin, tout)):
            tot[i] += v
    rows.append(f"| **all runs** | | | {tot[0]} | {tot[1]} | {tot[2]} | {tot[3]:,} | {tot[4]:,} | "
                "free tier: billed $0; list-price equivalent in the usage report |")
    return rows


def agreement_table(review: dict) -> list[str]:
    ds = REPO / "dataset"
    _o, _s, skipped = load_evidence(ds)
    aliases = resource_aliases(parse_activity(ds)[0])
    pngs = sorted((e for e in skipped if e.kind == "png"), key=lambda e: e.file_id)
    runs = [(k, v) for k, v in review["runs"].items() if v.get("dir")]
    head = "| File | " + " | ".join(f"{k} ({v['model']}, {v['prompt']})" for k, v in runs) + " |"
    rows = [head, "|---|" + "---|" * len(runs)]
    for ev in pngs:
        man = multimodal._manual_entry(ev, multimodal.MANUAL_DIR)
        cells = []
        for _k, v in runs:
            p = EVAL / "vlm_runs" / v["dir"] / f"{ev.file_id}.json"
            if not p.exists() or man is None:
                cells.append("no result")
                continue
            c = cmp.compare_records(man["records"], json.loads(p.read_text(encoding="utf-8"))["records"], aliases)
            cells.append(f"{len(c['matched'])}/{c['manual_n']} matched, {len(c['missing'])} missing, "
                         f"{len(c['extra'])} extra, {len(c['mismatches'])} field diffs; "
                         f"pipeline-identical: {'yes' if c['pipeline_identical'] else 'no'}")
        rows.append(f"| {ev.file_id} | " + " | ".join(cells) + " |")
    return rows


def review_table(review: dict) -> tuple[list[str], str]:
    name = sorted(review["reviews"])[-1]
    meta = review["runs"][name]
    rows = ["| File | Decision | Why (checked against the image) |", "|---|---|---|"]
    acc = 0
    for fid, r in sorted(review["reviews"][name].items()):
        acc += bool(r["accepted"])
        rows.append(f"| {fid} | {'VLM accepted' if r['accepted'] else 'manual kept (VLM rejected)'} | {r['reason']} |")
    n = len(review["reviews"][name])
    return rows, (f"{name} ({meta['model']}, prompt {meta['prompt']}): {acc} of {n} reviewed files verified, "
                  f"{n - acc} rejected. {meta['note']}.")


def main():
    review = json.loads((EVAL / "vlm_review.json").read_text(encoding="utf-8"))
    rtab, rsum = review_table(review)
    frozen = sorted(p.name for p in multimodal.FROZEN_DIR.glob("*.json")) if multimodal.FROZEN_DIR.is_dir() else []
    text = "\n".join([
        "<!-- Generated by tools/vlm_section.py from recorded runs and code/evaluation/vlm_review.json -->",
        "### Provenance of the screenshot observations",
        "",
        "The 11 PNG screenshots have no text layer. The committed observations come from **manual "
        "transcriptions (`code/multimodal_manual/`) that were checked against the images**. The vision "
        "model (VLM) was used as a draft and as a cross-check: each of its results was compared with the "
        "manual transcription and every difference was judged by opening the image. "
        + (f"{len(frozen)} VLM result(s) are frozen in `code/multimodal_vlm/` and used by the pipeline."
           if frozen else "No VLM result is frozen, so the pipeline output uses the manual transcriptions. ")
        + "PDFs are read with `pdftotext`, without a model.",
        "",
        "### Vision runs (models, prompt versions, tokens)", "",
        *runs_table(review), "",
        "Prompt versions: v2 first version; v3 per-category state guidance and no identifier copying; v4 catalog "
        "`system_id` and category from the resource-id prefix; v5 data flows/backups take only "
        "ACTIVE/DISABLED/BLOCKED, a retained reference without direct identifiers is REDACTED, linked records "
        "are listed. All wording is generic (see `code/parsers/NOTES.md`).", "",
        "### Agreement with the manual transcription, per file and run", "",
        *agreement_table(review), "",
        "### Review of the latest verified run against the images", "",
        rsum, "", *rtab, "",
    ])
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
