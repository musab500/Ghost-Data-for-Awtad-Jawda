"""Parser for SQLite-compatible SQL snapshot exports (dataset/evidence/databases).

Each file is executed into an in-memory SQLite database and every table is
introspected, so new table names are handled as long as rows carry a
``resource_id``. Three table families exist in the dataset:

* ``record_json`` tables (customer_data_primary / *_replica): one JSON business
  record per row plus origin_system and collection_name.
* inventory tables (customer_profiles, customer_search_documents, ...) with a
  ``snapshot_scope`` table listing which systems were captured, and when.
* flat profile/search tables (customer_profiles_primary / _failover_replica,
  search_documents_primary).

Every table is a separate *copy*; a resource present in a replica but missing
from the primary is replication drift, so Scope objects are emitted per table.
"""
from __future__ import annotations

import json
import re
import sqlite3

from models import Observation, Scope
from parsers.common import SYSTEM_FOR_CATEGORY, interpret
from parsers.manifest import EvidenceFile
from models import category_for_resource

INVENTORY_TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}
TIME_COLUMNS = ("updated_at", "indexed_at", "observed_at", "recorded_at")
CAPTURED_RE = re.compile(r"--\s*Captured at:\s*(\S+)", re.IGNORECASE)


def _load(path) -> tuple[sqlite3.Connection, str]:
    text = path.read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(text)
    m = CAPTURED_RE.search(text)
    return conn, (m.group(1) if m else "")


def parse_sql(ev: EvidenceFile) -> tuple[list[Observation], list[Scope]]:
    conn, header_time = _load(ev.path)
    snapshot_time = header_time or ev.captured_at
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    scope_times: dict[str, str] = {}
    if "snapshot_scope" in tables:
        for r in conn.execute("SELECT origin_system, captured_at FROM snapshot_scope"):
            scope_times[r[0]] = r[1]

    obs: list[Observation] = []
    scopes: list[Scope] = []
    for table in tables:
        if table == "snapshot_scope":
            continue
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        if "resource_id" not in cols:
            continue
        present: dict[str, set[str]] = {}   # system -> resource ids in this copy
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY resource_id'):
            row = dict(row)
            if "record_json" in row:
                rec = json.loads(row["record_json"])
                rec.setdefault("resource_id", row["resource_id"])
                info = interpret(rec, row.get("origin_system"), row.get("collection_name"))
                raw = rec
            else:
                system = INVENTORY_TABLE_SYSTEM.get(table)
                info = interpret(row, system, table)
                raw = row
            if info is None:
                continue
            system = info["system_id"] or SYSTEM_FOR_CATEGORY.get(
                category_for_resource(info["resource_id"]) or "", "unknown")
            present.setdefault(system, set()).add(info["resource_id"])
            # The row was *seen* at snapshot time; its own updated_at is kept in raw.
            ts = scope_times.get(system, snapshot_time)
            row_time = next((row[c] for c in TIME_COLUMNS if row.get(c)), "")
            obs.append(Observation(
                resource_id=info["resource_id"], system_id=system,
                category=info["category"], state=info["state"],
                identifiers=info["identifiers"], observed_at=ts,
                evidence_id=ev.file_id, source_kind="sql",
                raw={**raw, "_copy": table, "_row_time": row_time, "_names": sorted(info["names"]),
                     "_links": info["links"]},
                captured_at=ev.captured_at, location=f"{ev.rel_path}#{table}"))

        # Which systems does this table fully capture?
        if table in INVENTORY_TABLE_SYSTEM:
            system = INVENTORY_TABLE_SYSTEM[table]
            if system in scope_times:
                present.setdefault(system, set())
                scope_sys = {system: scope_times[system]}
            else:
                scope_sys = {}
        else:
            scope_sys = {s: snapshot_time for s in present}
        for system, t in scope_sys.items():
            scopes.append(Scope(evidence_id=ev.file_id, system_id=system,
                                observed_at=t, present=present.get(system, set()),
                                copy=table, captured_at=ev.captured_at,
                                source_kind="sql"))
    conn.close()
    return obs, scopes
