"""Fill the request-classifier cache with real model verdicts, paced, stopping at the first failure.

Calls the classifier model (llm_provider, e.g. Gemini) once per distinct request text of
dataset/deletion_requests.csv and dataset/sample_deletion_requests.csv that has no cached
verdict. Cached verdicts are never called again. On the first failed call (429 or anything
else) it stops and prints the exact message. Use --limit for a batch.

    python3 tools/classify_batch.py --limit 100 --pause 4.5
"""
import argparse
import csv
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "code"))

import classifier  # noqa: E402
import llm_provider  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max calls in this batch (0 = no limit)")
    ap.add_argument("--pause", type=float, default=4.5, help="seconds between calls")
    a = ap.parse_args()
    llm_provider.load_dotenv() if hasattr(llm_provider, "load_dotenv") else None
    clf = classifier.RequestClassifier()
    if not clf.use_llm:
        sys.exit("no provider configured (GEMINI_API_KEY): nothing was called")
    texts = []
    for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
        for r in csv.DictReader(open(REPO / "dataset" / name, encoding="utf-8")):
            if r["request_text"] not in texts:
                texts.append(r["request_text"])
    todo = [t for t in texts if clf._cache.get(clf._key(t)) is None]
    print(f"{len(texts)} distinct texts, {len(texts) - len(todo)} cached, {len(todo)} to call "
          f"(provider {clf.provider}, model {clf.model})")
    calls = 0
    for t in todo:
        if a.limit and calls >= a.limit:
            break
        if calls:
            time.sleep(a.pause)
        before = len(clf.llm_errors)
        clf.llm_verdict(t)
        calls += 1
        if len(clf.llm_errors) > before:
            print(f"STOP at call {calls}: {clf.llm_errors[-1]}")
            break
        if calls % 25 == 0:
            print(f"  {calls} calls, {len(clf._cache)} cached verdicts", flush=True)
    left = sum(1 for t in texts if clf._cache.get(clf._key(t)) is None)
    print(f"done: {calls} calls this batch, {left} texts still without a model verdict")


if __name__ == "__main__":
    main()
