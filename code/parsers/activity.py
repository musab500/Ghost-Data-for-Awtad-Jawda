"""Parser for dataset/system_activity.csv.

Most rows are workflow claims ("Deletion worker reported ... completed") and
are returned as ActivityEvent objects only; their reported_result is never
used as proof of state. Two kinds of rows are treated as evidence:

* LINK_RESOURCE_IDENTITY: a data catalog explicitly links a resource to an
  email and user_id -> ResourceLink.
* CHECK_DATA_FLOW / CHECK_RESTORE_ACCESS: a status check that *observed* the
  current state ("remains enabled", "can still be restored") -> Observation,
  citable by its activity_id.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from models import (ACTIVE, BLOCKED, DISABLED, ActivityEvent, Observation,
                    ResourceLink, category_for_resource, extract_identifiers)
from parsers.common import SYSTEM_FOR_CATEGORY

RID_RE = re.compile(r"\b(?:account|search|analytics|export|data_flow|backup|audit_record|"
                    r"legal_hold|tombstone|support|cache|marketing|object|telemetry)_\d+\b")


def parse_activity(dataset_dir: Path) -> tuple[list[ActivityEvent], list[Observation], list[ResourceLink]]:
    events: list[ActivityEvent] = []
    obs: list[Observation] = []
    links: list[ResourceLink] = []
    with open(Path(dataset_dir) / "system_activity.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rid = r["resource_id"].strip()
            related = sorted({m for m in RID_RE.findall(r["details"]) if m != rid})
            ev = ActivityEvent(r["activity_id"], r["event_time"], r["event_type"],
                               r["source_system"], r["target_system"], rid,
                               r["reported_result"], r["details"], related)
            events.append(ev)
            etype = r["event_type"]
            details = r["details"].lower()

            if etype == "LINK_RESOURCE_IDENTITY":
                links.append(ResourceLink(
                    resource_id=rid, system_id=r["target_system"],
                    identifiers=extract_identifiers(r["details"]),
                    observed_at=r["event_time"], evidence_id=r["activity_id"],
                    exclusive="belongs only to" in details))
                continue

            state = None
            if etype == "CHECK_DATA_FLOW":
                if "remains enabled" in details or "still enabled" in details:
                    state = ACTIVE
                elif "disabled" in details:
                    state = DISABLED
            elif etype == "CHECK_RESTORE_ACCESS":
                if "can still be restored" in details:
                    state = ACTIVE
                elif "blocked" in details or "cannot be restored" in details:
                    state = BLOCKED
            if state:
                cat = category_for_resource(rid)
                obs.append(Observation(
                    resource_id=rid,
                    system_id=r["target_system"] or SYSTEM_FOR_CATEGORY.get(cat or "", ""),
                    category=cat, state=state, identifiers=set(),
                    observed_at=r["event_time"], evidence_id=r["activity_id"],
                    source_kind="activity", raw=dict(r),
                    captured_at=r["event_time"], location="system_activity.csv"))
    return events, obs, links


def non_deletion_flags(events: list[ActivityEvent]) -> dict[str, ActivityEvent]:
    """{account resource_id: CLASSIFY_REQUEST event} for requests the privacy
    intake classified as non-deletion. This is a workflow claim: use it only
    to cross-check the request classifier, never as the decision itself."""
    return {e.resource_id: e for e in events
            if e.event_type == "CLASSIFY_REQUEST" and "non-deletion" in e.details.lower()}
