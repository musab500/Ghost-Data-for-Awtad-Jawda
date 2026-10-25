"""LLM vs fallback vs combined classifier comparison (Person B).

Needs ANTHROPIC_API_KEY (env or gitignored .env) unless every text is already in
code/.cache/classifier.json. Never fabricates: with no key and no cache it exits
non-zero and writes nothing. On success it rewrites the section of
evaluation_report.md between the CLASSIFIER_EVAL markers with the measured results.

Run: python3 code/evaluation/classifier_eval.py
"""
from __future__ import annotations
import csv, sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
from main import Pipeline                              # noqa: E402
from classifier import RequestClassifier, fallback_classify, _hard_negation  # noqa: E402
from parsers.activity import non_deletion_flags        # noqa: E402
from state import flagged_cases                        # noqa: E402

REPORT = CODE / "evaluation" / "evaluation_report.md"
START, END = "<!-- CLASSIFIER_EVAL_START -->", "<!-- CLASSIFIER_EVAL_END -->"


def main() -> int:
    ds = CODE.parent / "dataset"
    clf = RequestClassifier()
    pipe = Pipeline(ds, use_llm=clf.use_llm)
    clf = pipe.classifier
    full = pipe._full_case_ids()
    texts = {c: pipe.requests[c]["request_text"] for c in pipe.requests}
    uniq = sorted({t for t in texts.values()})

    llm = {}
    for t in uniq:
        v = clf.llm_verdict(t)
        if v is None:
            print(f"ERROR: no LLM verdict for a request text (no API key / call failed / not cached). "
                  f"{len(clf.llm_errors)} error(s). Nothing written.", file=sys.stderr)
            return 2
        llm[t] = v

    flags = non_deletion_flags(pipe.events)
    flagged = set(flagged_cases(pipe.direct, flags)) & set(full)
    modes = {
        "LLM only": lambda t: llm[t].is_deletion,
        "Fallback only": lambda t: fallback_classify(t).is_deletion,
        "Combined (LLM + negation override)": lambda t: clf.classify(t).is_deletion,
    }
    rows = []
    for name, fn in modes.items():
        na = {c for c in full if not fn(texts[c])}
        rows.append((name, len(na), len(na & flagged), len(na - flagged), len(flagged - na)))
    sample = [c for c in pipe.requests if c not in set(full)]
    dis = sorted({t for t in uniq if llm[t].is_deletion != fallback_classify(t).is_deletion})

    out = [START, "", "### Classifier comparison (measured)", "",
           f"Model: `{clf.model}`; prompt version `{__import__('classifier').PROMPT_VERSION}`; "
           f"{len(uniq)} unique request texts (518 evaluation + 32 sample requests).",
           f"CLASSIFY_REQUEST workflow flags on the 518 set: {len(flagged)} (used as a cross-check only).", "",
           "| Mode | NOT_APPLICABLE predicted | Agree with flags | Extra (false positive vs flags) | Missed |",
           "|---|---|---|---|---|"]
    out += [f"| {n} | {a} | {b} | {c} | {d} |" for n, a, b, c, d in rows]
    sfp = sorted(c for c in sample if not modes["Combined (LLM + negation override)"](texts[c]))
    out += ["", f"Sample requests classified NOT_APPLICABLE by the combined classifier: {sfp or 'none'} "
            f"(all 32 samples are deletion cases).", "",
            f"Texts where LLM and fallback disagree: **{len(dis)}**"]
    for t in dis:
        out.append(f"- LLM={'deletion' if llm[t].is_deletion else 'non-deletion'} "
                   f"({llm[t].reason}); fallback={'deletion' if fallback_classify(t).is_deletion else 'non-deletion'}; "
                   f"negation phrase={_hard_negation(t)!r}: \"{t[:200]}\"")
    out += ["", END]
    block = "\n".join(out)
    md = REPORT.read_text(encoding="utf-8")
    if START in md and END in md:
        md = md[:md.index(START)] + block + md[md.index(END) + len(END):]
    else:
        md = md.rstrip() + "\n\n## 10. Classifier: LLM vs fallback\n\n" + block + "\n"
    REPORT.write_text(md, encoding="utf-8")
    print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
