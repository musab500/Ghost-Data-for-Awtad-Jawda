"""Parser for plain-text records (e.g. shift handover notes).

Check lines look like:
    - 2026-10-01T12:00:00Z | search_service | search_052 | SEARCH_INDEX_ENTRY | FOUND | user@example.com
i.e. time | system | resource | category | result | identifier. These are
explicit check results captured by the evidence source.
"""
from __future__ import annotations

import re

from models import Observation, normalize_identifier
from parsers.common import RESULT_STATES
from parsers.manifest import EvidenceFile

LINE_RE = re.compile(
    r"^\s*[-*]?\s*(?P<ts>\d{4}-\d{2}-\d{2}T[\d:]+Z)\s*\|\s*(?P<system>[\w.-]+)\s*\|"
    r"\s*(?P<rid>[\w.-]+)\s*\|\s*(?P<cat>[A-Z_]+)\s*\|\s*(?P<state>[A-Z_]+)\s*\|\s*(?P<ident>.*?)\s*$")


def parse_txt(ev: EvidenceFile) -> list[Observation]:
    obs = []
    for lineno, line in enumerate(ev.path.read_text(encoding="utf-8").splitlines(), 1):
        m = LINE_RE.match(line)
        if not m:
            continue
        state = m["state"]
        if state not in RESULT_STATES:
            raise ValueError(f"{ev.rel_path}:{lineno}: unknown state {state!r}")
        ident = normalize_identifier(m["ident"])
        obs.append(Observation(
            resource_id=m["rid"], system_id=m["system"], category=m["cat"],
            state=state, identifiers={ident} if ident else set(),
            observed_at=m["ts"], evidence_id=ev.file_id, source_kind="txt",
            raw={"line": line.strip()}, captured_at=ev.captured_at,
            location=f"{ev.rel_path}:{lineno}"))
    return obs
