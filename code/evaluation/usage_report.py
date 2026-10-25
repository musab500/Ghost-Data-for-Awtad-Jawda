"""Generate evaluation/usage_report.md from REAL recorded usage (Person B).

Every number is computed here from recorded files; nothing is typed by hand or estimated.

Inputs (all optional; absence is reported, never filled in):
  code/.cache/classifier_usage.json   one record per classifier API response
  multimodal_usage.json               Person A's vision usage, first found of:
                                      code/.cache/, code/, code/evaluation/
  code/.cache/last_run.json           manifest written by the latest main.py run
  code/evaluation/prices.json         per-MTok prices keyed "provider/model", with source and date
A usage record needs input_tokens and output_tokens; "provider" and "model" identify the row (a
record without "provider" is attributed from its model name). One report row per provider+model.

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
MULTIMODAL_USAGE_CANDIDATES = [CACHE / "multimodal_usage.json",
                               CODE / "multimodal_usage.json",
                               CODE / "evaluation" / "multimodal_usage.json"]


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _records(data):
    if data is None:
        return None
    if isinstance(data, dict):
        data = data.get("calls", data.get("records", []))
    return [r for r in data if isinstance(r, dict)]


def _provider_of(rec: dict) -> str:
    if rec.get("provider"):
        return str(rec["provider"])
    m = str(rec.get("model", "")).lower()
    return "anthropic" if m.startswith("claude") else "gemini" if m.startswith(("gemini", "gemma")) else "unknown"


def _price(prices: dict, provider: str, model: str):
    best = None
    for k, v in prices.get("models", {}).items():
        kp, _, km = k.partition("/")
        if kp == provider and (model == km or model.startswith(km)):
            if best is None or len(km) > len(best[0]):
                best = (km, v)
    return best[1] if best else None


def _cost(entry, tin: int, tout: int):
    if not entry or entry.get("input") is None or entry.get("output") is None:
        return None
    return (tin * entry["input"] + tout * entry["output"]) / 1e6


def collect() -> dict:
    """All usage figures as plain data (also used by evaluation/main.py)."""
    prices = _load(CODE / "evaluation" / "prices.json") or {}
    manifest = _load(CACHE / "last_run.json") or {}
    tier = prices.get("gemini_tier", "unknown")
    mm_path = next((p for p in MULTIMODAL_USAGE_CANDIDATES if p.exists()), None)
    sources = [("Request classifier", _records(_load(CACHE / "classifier_usage.json")), "classifier_usage.json"),
               ("Multimodal (images/PDFs)", _records(_load(mm_path)) if mm_path else None, "multimodal_usage.json")]
    per = defaultdict(lambda: {"calls": 0, "failed": 0, "input": 0, "output": 0, "unreported": 0, "components": set()})
    by_component = defaultdict(lambda: {"calls": 0, "tokens": 0})
    missing = []
    for comp, recs, fname in sources:
        if recs is None:
            missing.append(f"{comp}: no `{fname}` found, so 0 recorded calls.")
            continue
        for r in recs:
            e = per[(_provider_of(r), str(r.get("model", "unknown")))]
            e["calls"] += 1
            e["failed"] += int(r.get("ok") is False)
            e["input"] += int(r.get("input_tokens", 0))
            e["output"] += int(r.get("output_tokens", 0))
            e["unreported"] += int(r.get("usage_reported") is False)
            e["components"].add(comp)
            by_component[comp]["calls"] += 1
            by_component[comp]["tokens"] += int(r.get("input_tokens", 0)) + int(r.get("output_tokens", 0))
    rows, cost_total, cost_ok = [], 0.0, True
    tot = {"calls": 0, "input": 0, "output": 0}
    for (prov, model), e in sorted(per.items()):
        entry = _price(prices, prov, model)
        paid = _cost(entry, e["input"], e["output"])
        free = prov == "gemini" and tier == "free"
        cost = 0.0 if free else paid
        if cost is None:
            cost_ok = False
        else:
            cost_total += cost
        tot["calls"] += e["calls"]
        tot["input"] += e["input"]
        tot["output"] += e["output"]
        rows.append({"provider": prov, "model": model, **{k: e[k] for k in ("calls", "failed", "input", "output", "unreported")},
                     "components": sorted(e["components"]), "cost": cost, "free_tier": free, "paid_reference": paid if free else None,
                     "price_source": (entry or {}).get("source"), "price_retrieved": (entry or {}).get("retrieved")})
    return {"prices": prices, "manifest": manifest, "rows": rows, "missing": missing, "tier": tier,
            "by_component": dict(by_component), "calls": tot["calls"], "input": tot["input"],
            "output": tot["output"], "cost_total": cost_total, "cost_ok": cost_ok}


def _money(v) -> str:
    return "price not set" if v is None else f"${v:.6f}"


def summary_line(d: dict | None = None) -> str:
    d = d or collect()
    cost = f"${d['cost_total']:.6f}" if d["cost_ok"] else "incomplete (a model price is not set)"
    return (f"{d['calls']} recorded model call(s), {d['input']:,} input / {d['output']:,} output tokens, "
            f"cost {cost}; classifier mode in the latest run: {d['manifest'].get('classifier_mode', 'unknown')}")


def render(d: dict) -> str:
    m, prices = d["manifest"], d["prices"]
    lines = ["# Token Usage and Cost Report — Ghost Data Still Exists", "",
             "<!-- Generated by code/evaluation/usage_report.py; do not edit by hand. -->",
             f"Summarizes the latest full-dataset run of `code/main.py` ({m.get('rows', N_REQ)} requests). "
             "Every figure is read from recorded API responses; nothing is estimated.", "",
             "## 1. How the run used models", ""]
    if m:
        mode = m.get("classifier_mode")
        lines += [f"- Classifier mode: **{mode}**"
                  + (f" (`{m.get('classifier_provider')}` / `{m.get('classifier_model')}`)" if mode == "llm" else " (no model)")
                  + (f"; LLM switched off mid-run: {m['llm_disabled_reason']}" if m.get("llm_disabled_reason") else "") + ".",
                  f"- New classifier API responses in that run: {m.get('new_llm_calls')}; "
                  f"served from the on-disk cache: {m.get('llm_cache_hits')}; failures: {m.get('llm_errors')}.",
                  f"- Multimodal observations loaded: {m.get('multimodal_observations')}.",
                  "- Every other stage (identity, parsing, state, policy, steps, validation) is deterministic and makes no model call.",
                  "- Cached verdicts were paid for once, when the cache was populated; their recorded usage is reported below."]
    else:
        lines.append("- No `last_run.json` found: run `python3 code/main.py` first.")
    lines += ["", "## 2. Usage and cost per provider and model", ""]
    if d["rows"]:
        lines += ["| Provider | Model | Responses | of which failed | Input tokens | Output tokens | Total tokens | Cost (USD) |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in d["rows"]:
            if r["free_tier"]:
                c = "$0.000000 (free tier)" + (f"; paid-tier reference {_money(r['paid_reference'])}" if r["paid_reference"] is not None else "")
            else:
                c = _money(r["cost"])
            lines.append(f"| {r['provider']} | `{r['model']}` | {r['calls']} | {r['failed']} | {r['input']:,} | "
                         f"{r['output']:,} | {r['input'] + r['output']:,} | {c} |")
        oc = f"${d['cost_total']:.6f}" if d["cost_ok"] else "incomplete (a model price is not set)"
        lines.append(f"| **Overall** | | **{d['calls']}** | | **{d['input']:,}** | **{d['output']:,}** | "
                     f"**{d['input'] + d['output']:,}** | **{oc}** |")
        lines += ["", "Failed responses (blocked, truncated, malformed) are included because their tokens were billed. "
                      "Output tokens include Gemini thinking tokens, which are billed as output."]
        unrep = sum(r["unreported"] for r in d["rows"])
        if unrep:
            lines.append(f"{unrep} response(s) carried no usage metadata; their tokens are not counted.")
        if d["by_component"]:
            lines += ["", "| Component | Responses | Tokens |", "|---|---|---|"]
            lines += [f"| {k} | {v['calls']} | {v['tokens']:,} |" for k, v in sorted(d["by_component"].items())]
    else:
        lines.append("No model calls were recorded. Overall: 0 calls, 0 tokens, $0.00.")
    tok = d["input"] + d["output"]
    lines += ["", f"## 3. Per-request averages (÷ {N_REQ} requests)", "",
              f"- Total tokens per request: {tok / N_REQ:,.2f}",
              f"- Input tokens per request: {d['input'] / N_REQ:,.2f}; output tokens per request: {d['output'] / N_REQ:,.2f}",
              f"- Cost per request: {('$%.8f' % (d['cost_total'] / N_REQ)) if d['cost_ok'] else 'n/a (a model price is not set)'}", "",
              "## 4. Prices used", "", f"{prices.get('unit', '')}. Gemini tier: **{d['tier']}**.", ""]
    for k, v in prices.get("models", {}).items():
        if v.get("input") is None:
            ref = v.get("unverified_reference")
            lines.append(f"- `{k}`: **official price not set** (source: {v.get('source')})."
                         + (f" Unverified third-party reference, NOT used in any cost: ${ref['input']} in / ${ref['output']} out." if ref else ""))
        else:
            lines.append(f"- `{k}`: ${v['input']} input / ${v['output']} output per MTok (source: {v.get('source')}, retrieved {v.get('retrieved')})")
    if d["missing"]:
        lines += ["", "## 5. Notes", ""] + [f"- {x}" for x in d["missing"]]
    vlm = CODE / "evaluation" / "vlm_section.md"        # generated by tools/vlm_section.py (Person A)
    if vlm.exists():
        lines += ["", "## 6. Vision-model (VLM) runs", "", vlm.read_text(encoding="utf-8").strip()]
    lines += ["", "No API keys or credentials appear in this report.", ""]
    return "\n".join(lines)


def main() -> int:
    text = render(collect())
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}: {summary_line()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
