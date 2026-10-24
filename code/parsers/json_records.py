"""Parser for JSON evidence files (dataset/evidence/records/*.json).

The JSON files come in several layouts (snapshot bundles, per-service resource
exports, single-collection reports, lists of explicit check results). Rather
than hard-coding each layout, the parser walks the document and treats every
object with a ``resource_id`` as a record. Context such as the system, the
collection and the observation time is inherited from the nearest enclosing
object (``data_store`` / ``system`` / service key, ``collection``,
``observed_at`` / ``recorded_at`` / ``captured_at`` / ``exported_at``).

A container that holds the records of one system/collection captured at one
time (a snapshot entry, a service export section, a single-collection report)
is emitted as a Scope so that missing resources can be proven absent.
"""
from __future__ import annotations

import json

from models import Observation, Scope
from parsers.common import SYSTEM_FOR_CATEGORY, interpret
from parsers.manifest import EvidenceFile
from models import category_for_resource

TIME_KEYS = ("observed_at", "recorded_at", "captured_at", "exported_at")
KNOWN_SYSTEMS = set(SYSTEM_FOR_CATEGORY.values())


def _ctx_time(d: dict) -> str | None:
    for k in ("captured_at", "exported_at"):
        if isinstance(d.get(k), str):
            return d[k]
    return None


def parse_json(ev: EvidenceFile) -> tuple[list[Observation], list[Scope]]:
    doc = json.loads(ev.path.read_text(encoding="utf-8"))
    obs: list[Observation] = []
    scopes: list[Scope] = []

    def emit(rec: dict, system: str | None, coll: str | None, t: str, path: str):
        info = interpret(rec, system, coll)
        if info is None:
            return None
        rec_time = next((rec[k] for k in TIME_KEYS if isinstance(rec.get(k), str)), t)
        sys_id = info["system_id"] or SYSTEM_FOR_CATEGORY.get(
            category_for_resource(info["resource_id"]) or "", "unknown")
        obs.append(Observation(
            resource_id=info["resource_id"], system_id=sys_id,
            category=info["category"], state=info["state"],
            identifiers=info["identifiers"], observed_at=rec_time,
            evidence_id=ev.file_id, source_kind="json",
            raw={**rec, "_collection": coll, "_names": sorted(info["names"]),
                 "_links": info["links"]},
            captured_at=ev.captured_at, location=f"{ev.rel_path}#{path}"))
        return info

    def walk(node, system, coll, t, path):
        if isinstance(node, dict):
            if "resource_id" in node:
                emit(node, system, coll, t, path)
                return
            t = _ctx_time(node) or t
            system = node.get("data_store") or node.get("system") or system
            coll = node.get("collection") or coll
            for k, v in node.items():
                sub_system = k if k in KNOWN_SYSTEMS else system
                walk(v, sub_system, coll, t, f"{path}.{k}")
                if isinstance(v, list) and _is_record_list(v, node):
                    _scope_for(v, sub_system, coll, t, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, system, coll, t, f"{path}[{i}]")

    def _is_record_list(lst, parent) -> bool:
        # Lists of explicit check rows are not complete captures of a system.
        if any(isinstance(r, dict) and "result" in r for r in lst):
            return False
        return all(isinstance(r, dict) and "resource_id" in r for r in lst)

    def _scope_for(lst, system, coll, t, path):
        by_sys: dict[str, set[str]] = {}
        for r in lst:
            info = interpret(r, system, coll)
            if info and info["system_id"]:
                by_sys.setdefault(info["system_id"], set()).add(info["resource_id"])
        if not lst and system:
            by_sys[system] = set()
        for s, present in by_sys.items():
            scopes.append(Scope(evidence_id=ev.file_id, system_id=s, observed_at=t,
                                present=present, copy=coll or path,
                                captured_at=ev.captured_at, source_kind="json"))

    walk(doc, None, None, ev.captured_at, "$")
    return obs, scopes
