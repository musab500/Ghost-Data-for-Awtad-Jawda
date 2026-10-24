"""Evidence parsers. ``load_evidence`` parses every manifest file once.

Images and PDFs are handled by multimodal.py (Person B); files whose format
has no parser are reported in ``skipped`` rather than silently ignored.
"""
from __future__ import annotations

from pathlib import Path

from models import Observation, Scope
from parsers.json_records import parse_json
from parsers.logs import parse_log
from parsers.manifest import EvidenceFile, load_manifest
from parsers.sql import parse_sql
from parsers.txt import parse_txt


def load_evidence(dataset_dir: Path) -> tuple[list[Observation], list[Scope], list[EvidenceFile]]:
    observations: list[Observation] = []
    scopes: list[Scope] = []
    skipped: list[EvidenceFile] = []
    for ev in load_manifest(dataset_dir):
        if ev.kind == "sql":
            o, s = parse_sql(ev)
        elif ev.kind == "json":
            o, s = parse_json(ev)
        elif ev.kind == "log":
            o, s = parse_log(ev), []
        elif ev.kind == "txt":
            o, s = parse_txt(ev), []
        else:
            skipped.append(ev)
            continue
        observations += o
        scopes += s
    return observations, scopes, skipped
