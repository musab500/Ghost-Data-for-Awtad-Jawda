"""Shared helpers for the team-authored evaluation cases.

Each case builds a tiny in-memory EvidenceIndex from hand-made Observations,
so it tests one slice of the decision logic in isolation — without depending on
the full evidence corpus or on any golden label.
"""
from __future__ import annotations

import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = CODE_DIR.parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from models import Observation  # noqa: E402
from rules import load_rules  # noqa: E402

RULES = load_rules(REPO_ROOT / "dataset")


def obs(resource_id, system_id, category, state, when, evidence_id,
        identifiers=(), raw=None):
    """An Observation whose observed_at and captured_at are both `when`."""
    return Observation(
        resource_id=resource_id, system_id=system_id, category=category,
        state=state, identifiers=set(identifiers), observed_at=when,
        evidence_id=evidence_id, source_kind="sql", raw=raw or {},
        captured_at=when,
    )
