"""Merge several AGENTS.md log files into ONE chronological log.txt.

* Entries are never edited. Each entry's text is copied verbatim.
* Entries are never dropped: by default identical entries that appear in more than
  one input (e.g. shared history) are kept and reported; --dedupe-identical collapses
  byte-identical entries and says how many it collapsed.
* Ordering is by the timestamp in each header (UTC); ties keep input order.
* Every entry must contain exactly one non-empty tool= line and exactly one non-empty
  model= line (no placeholders / "unknown"). Invalid entries make the exit code non-zero
  but are still written, so nothing is lost.
* No marker is added to entries. Per-branch counts come from the existing branch= field
  (or the "Branch:" line of SESSION START entries).
* After writing, the output is re-parsed and compared with the inputs; any difference
  fails loudly.

Usage:
  python3 tools/merge_logs.py log.txt other_log.txt --out merged_log.txt
  python3 tools/merge_logs.py log.txt --check          # verify + count only
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HEADER = re.compile(
    r"^## \[?(?P<ts>\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)\]?[ \t]*(?P<title>.*)$")
BAD_VALUES = {"", "unknown", "none", "n/a", "ai", "generic", "tbd"}


class Entry:
    def __init__(self, text: str, ts: datetime, title: str, source: str, order: int, line: int):
        self.text, self.ts, self.title = text, ts, title
        self.source, self.order, self.line = source, order, line

    def field(self, name: str) -> list[str]:
        return [m.strip() for m in re.findall(rf"^{name}=(.*)$", self.text, flags=re.M)]

    @property
    def branch(self) -> str:
        b = self.field("branch")
        if b:
            return b[0] or "(empty)"
        m = re.search(r"^Branch:[ \t]*(\S+)", self.text, flags=re.M)
        return m.group(1) if m else "(none)"

    def problems(self) -> list[str]:
        out = []
        for name in ("tool", "model"):
            vals = self.field(name)
            if len(vals) != 1:
                out.append(f"{len(vals)} {name}= lines (need exactly 1)")
            elif vals[0].lower() in BAD_VALUES or re.search(r"[<>]|\[.*\]", vals[0]):
                out.append(f"{name}= is empty / placeholder / unknown: {vals[0]!r}")
        return out


def parse_ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-2] + ":" + s[-2:]
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_file(path: Path, file_order: int) -> tuple[str, list[Entry]]:
    lines = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    starts = [(i, HEADER.match(l)) for i, l in enumerate(lines) if HEADER.match(l)]
    preamble = "\n".join(lines[:starts[0][0]]).strip("\n") if starts else "\n".join(lines).strip("\n")
    entries = []
    for k, (i, m) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        text = "\n".join(lines[i:end]).rstrip("\n")
        entries.append(Entry(text, parse_ts(m["ts"]), m["title"], str(path), file_order * 100000 + k, i + 1))
    return preamble, entries


def merge(paths: list[Path], dedupe: bool = False):
    preambles, entries = [], []
    for fi, p in enumerate(paths):
        pre, ents = parse_file(p, fi)
        if pre.strip():
            preambles.append(pre)
        entries += ents
    entries.sort(key=lambda e: (e.ts, e.order))
    dupes = 0
    if dedupe:
        seen, kept = set(), []
        for e in entries:
            if e.text in seen:
                dupes += 1
                continue
            seen.add(e.text)
            kept.append(e)
        entries = kept
    return preambles, entries, dupes


def render(preambles: list[str], entries: list[Entry]) -> str:
    parts = list(preambles) + [e.text for e in entries]
    return "\n\n".join(parts) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, help="write the merged log here")
    ap.add_argument("--check", action="store_true", help="verify and report only; write nothing")
    ap.add_argument("--dedupe-identical", action="store_true",
                    help="collapse byte-identical entries from different inputs (reported)")
    ap.add_argument("--allow-overwrite", action="store_true", help="allow --out to be one of the inputs")
    args = ap.parse_args(argv)
    if not args.check and not args.out:
        ap.error("give --out PATH (or --check)")
    if args.out and args.out.resolve() in {p.resolve() for p in args.logs} and not args.allow_overwrite:
        ap.error("--out is one of the inputs; choose another path or pass --allow-overwrite")

    pre, entries, collapsed = merge(args.logs, args.dedupe_identical)
    # duplicates present across inputs even when not collapsed
    texts = Counter(e.text for e in entries)
    dup_present = sum(c - 1 for c in texts.values() if c > 1)
    bad = [(e, p) for e in entries for p in e.problems()]

    print(f"inputs: {', '.join(str(p) for p in args.logs)}")
    print(f"entries: {len(entries)}  (collapsed identical: {collapsed}; identical still present: {dup_present})")
    for b, n in sorted(Counter(e.branch for e in entries).items()):
        print(f"  branch={b}: {n} entries")
    for s, n in sorted(Counter(Path(e.source).name for e in entries).items()):
        print(f"  from {s}: {n} entries")
    if bad:
        print(f"INVALID ENTRIES: {len(bad)}")
        for e, p in bad:
            print(f"  - {Path(e.source).name}:{e.line} [{e.ts:%Y-%m-%dT%H:%M:%SZ}] {e.title[:60]}: {p}")
    else:
        print("every entry has exactly one non-empty tool= and model= line")

    if not args.check:
        out_text = render(pre, entries)
        # integrity: parse the output back and compare with what we meant to write
        tmp = args.out.with_suffix(args.out.suffix + ".tmp")
        tmp.write_text(out_text, encoding="utf-8")
        _, again = parse_file(tmp, 0)
        if [e.text for e in again] != [e.text for e in entries]:
            tmp.unlink()
            print("FATAL: re-parsed output differs from the merged entries; nothing written", file=sys.stderr)
            return 2
        tmp.replace(args.out)
        # no input entry may be missing from the output (unless deliberately collapsed)
        all_in = Counter(e.text for p in args.logs for e in parse_file(p, 0)[1])
        out_ct = Counter(e.text for e in again)
        lost = sum(max(0, c - out_ct[t]) for t, c in all_in.items())
        if lost != collapsed:
            print(f"FATAL: {lost} input entries missing from output (expected {collapsed} collapsed)", file=sys.stderr)
            return 2
        print(f"wrote {args.out}: {len(again)} entries, chronological, none edited")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
