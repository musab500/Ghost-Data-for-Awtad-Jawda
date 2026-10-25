import subprocess, sys
from pathlib import Path

TOOL = Path(__file__).with_name("merge_logs.py")

def entry(ts, title, tool="Claude Code", model="claude-x", branch="b"):
    return f"## {ts} {title}\n\nbody\n\nContext:\ntool={tool}\nmodel={model}\nbranch={branch}\n"

def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True, text=True)

def test_merge_sorts_keeps_everything_and_counts_branches(tmp_path):
    shared = entry("2026-01-01T00:00:00Z", "shared", branch="main")
    a = tmp_path / "a.txt"; b = tmp_path / "b.txt"; out = tmp_path / "m.txt"
    a.write_text(shared + "\n" + entry("2026-01-03T00:00:00Z", "A late", branch="person-a"))
    b.write_text(shared + "\n" + entry("2026-01-02T00:00:00Z", "B mid", branch="person-b"))
    r = run(a, b, "--out", out)
    assert r.returncode == 0, r.stdout + r.stderr
    text = out.read_text()
    assert text.count("## 2026") == 4                      # nothing dropped, shared kept twice
    order = [l.split(" ", 2)[2] for l in text.splitlines() if l.startswith("## 2026")]
    assert order == ["shared", "shared", "B mid", "A late"]
    assert "branch=person-a: 1" in r.stdout and "branch=person-b: 1" in r.stdout
    r2 = run(a, b, "--out", tmp_path / "d.txt", "--dedupe-identical")
    assert (tmp_path / "d.txt").read_text().count("## 2026") == 3 and "collapsed identical: 1" in r2.stdout

def test_invalid_entries_are_reported_but_kept(tmp_path):
    f = tmp_path / "f.txt"; out = tmp_path / "o.txt"
    f.write_text(entry("2026-01-01T00:00:00Z", "ok") + "\n" + entry("2026-01-02T00:00:00Z", "no model", model="unknown")
                 + "\n## [2026-01-03T00:00:00Z] missing tool\n\nmodel=claude-x\n")
    r = run(f, "--out", out)
    assert r.returncode == 1 and "INVALID ENTRIES: 2" in r.stdout
    assert out.read_text().count("## ") == 3                # still written, none dropped

def test_refuses_to_overwrite_an_input(tmp_path):
    f = tmp_path / "f.txt"; f.write_text(entry("2026-01-01T00:00:00Z", "x"))
    assert run(f, "--out", f).returncode != 0
