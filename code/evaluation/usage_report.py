"""Generate evaluation/usage_report.md from REAL recorded usage (Person B).

Inputs (all optional; absence is reported, never estimated):
  code/.cache/classifier_usage.json   one record per classifier API call
  code/.cache/multimodal_usage.json   Person A's VLM usage (list of records or {"calls":[...]})
  code/.cache/last_run.json           manifest written by the final main.py run
  code/evaluation/prices.json         published per-MTok prices with source and date
Run: python3 code/evaluation/usage_report.py
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
CACHE = CODE / ".cache"
OUT = CODE / "evaluation" / "usage_report.md"
N_REQ = 518


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _records(data):
    if data is None: return None
    if isinstance(data, dict): data = data.get("calls", data.get("records", []))
    return [r for r in data if isinstance(r, dict)]


def _price(prices, model):
    best = None
    for k, v in prices.get("models", {}).items():
        if model == k or model.startswith(k):
            if best is None or len(k) > len(best[0]): best = (k, v)
    return best[1] if best else None


def main() -> int:
    prices = _load(CODE / "evaluation" / "prices.json") or {}
    manifest = _load(CACHE / "last_run.json") or {}
    sources = [("Request classifier", _records(_load(CACHE / "classifier_usage.json"))),
               ("Multimodal (images/PDFs)", _records(_load(CACHE / "multimodal_usage.json")))]
    per = defaultdict(lambda: [0, 0, 0, None])       # (component, model) -> calls,in,out,cost
    notes = []
    for comp, recs in sources:
        if recs is None:
            notes.append(f"- {comp}: no usage file found, so **0 recorded calls**.")
            continue
        for r in recs:
            m = r.get("model", "unknown"); e = per[(comp, m)]
            e[0] += 1; e[1] += int(r.get("input_tokens", 0)); e[2] += int(r.get("output_tokens", 0))
    lines = ["# Token Usage and Cost Report — Ghost Data Still Exists", "",
             "Summarizes the final full-dataset run that produced `output.csv` "
             f"({manifest.get('rows', N_REQ)} requests). All figures are read from recorded API "
             "responses; nothing here is estimated.", "",
             "## 1. How the final run used models", ""]
    if manifest:
        lines += [f"- Classifier mode in the final run: **{manifest.get('classifier_mode')}** "
                  f"(`{manifest.get('classifier_model')}`).",
                  f"- New classifier API calls in that run: {manifest.get('new_llm_calls')}; "
                  f"served from the on-disk cache: {manifest.get('llm_cache_hits')}; "
                  f"failures: {manifest.get('llm_errors')}.",
                  f"- Multimodal observations loaded: {manifest.get('multimodal_observations')}.",
                  "- Every other stage (identity, parsing, state, policy, steps, validation) is deterministic and makes no model call.",
                  "- Cached calls were paid for once, when the cache was populated; their recorded usage is reported below."]
    else:
        lines.append("- No `last_run.json` found: run `python3 code/main.py` first.")
    lines += ["", "## 2. Usage and cost per model", ""]
    tot = [0, 0, 0]; cost_total = 0.0; cost_ok = True
    if per:
        lines += ["| Component | Model | Calls | Input tokens | Output tokens | Total tokens | Cost (USD) |",
                  "|---|---|---|---|---|---|---|"]
        for (comp, m), e in sorted(per.items()):
            pr = _price(prices, m)
            cost = (e[1] * pr["input"] + e[2] * pr["output"]) / 1e6 if pr else None
            if cost is None: cost_ok = False
            else: cost_total += cost
            tot[0] += e[0]; tot[1] += e[1]; tot[2] += e[2]
            lines.append(f"| {comp} | `{m}` | {e[0]} | {e[1]:,} | {e[2]:,} | {e[1]+e[2]:,} | "
                         f"{('$%.6f' % cost) if cost is not None else 'price unknown'} |")
        lines.append(f"| **Overall** | | **{tot[0]}** | **{tot[1]:,}** | **{tot[2]:,}** | **{tot[1]+tot[2]:,}** | "
                     f"**{('$%.6f' % cost_total) if cost_ok else 'incomplete (a model price is unknown)'}** |")
    else:
        lines.append("No model calls were recorded. Overall: 0 calls, 0 tokens, $0.00.")
    lines += ["", "## 3. Per-request averages (÷ %d requests)" % N_REQ, "",
              f"- Total tokens per request: {(tot[1]+tot[2])/N_REQ:,.2f}",
              f"- Input tokens per request: {tot[1]/N_REQ:,.2f}; output tokens per request: {tot[2]/N_REQ:,.2f}",
              f"- Cost per request: {('$%.8f' % (cost_total/N_REQ)) if cost_ok else 'n/a'}", ""]
    lines += ["## 4. Prices used", "",
              f"Source: {prices.get('source','(prices.json missing)')}; retrieved {prices.get('retrieved','?')}; "
              f"{prices.get('unit','')}.", ""]
    for k, v in prices.get("models", {}).items():
        lines.append(f"- `{k}`: ${v['input']} input / ${v['output']} output per MTok")
    if notes: lines += ["", "## 5. Notes", ""] + notes
    lines += ["", "No API keys or credentials appear in this report.", ""]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
