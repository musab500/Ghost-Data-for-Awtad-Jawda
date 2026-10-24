"""Parser for application / observability logs (dataset/evidence/logs/*.log).

The logs use several layouts (logfmt, bracketed, pipe-separated, syslog,
JSON payloads, logfmt nested in a quoted ``message``). ``parse_line`` reduces
every layout to (timestamp, service, trace_id, event, fields).

Lines are then grouped by trace id: the ``*_started`` line of a trace usually
carries the lookup key (an email / user id) while the ``*_completed`` line
carries the resource id and the outcome, so identifiers are shared across all
lines of a trace. Each outcome line becomes one Observation.
"""
from __future__ import annotations

import json
import re

from models import (ACTIVE, ANONYMIZED, BLOCKED, DISABLED, FOUND, IDENTIFIABLE,
                    NOT_FOUND, REDACTED, Observation, category_for_resource,
                    extract_identifiers, normalize_identifier)
from parsers.common import RESULT_STATES, SYSTEM_FOR_CATEGORY
from parsers.manifest import EvidenceFile

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s*(.*)$")
KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
BRACKET_RE = re.compile(r"^\[(\w+)\]\s+\[([\w.-]+)\]\s+\[([\w.-]+)\]\s+(\w+):\s*(.*)$")
PIPE_RE = re.compile(r"^\|?\s*(\w+)\s*\|\s*([\w.-]+)\s*\|\s*([\w.-]+)\s*\|\s*(\w+)\s*\|\s*(.*)$")
SYSLOG_RE = re.compile(r"^<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+-\s+-\s+(.*)$")

# Fields that carry identifiers of the data subject.
ID_KEYS = ("lookup_key", "match_key", "subject_key", "scan_key", "subject_ref",
           "correlation_key", "matched_value", "customer_email", "email",
           "customer_reference", "subject_reference", "lookup_value", "phone",
           "email_hash", "owner_email", "owner_phone", "requester_email",
           "requester_phone", "subject_selector", "uploaded_by", "user_id")
DIRECT_KEYS = ("email", "phone", "email_hash", "customer_email", "owner_email",
               "owner_phone", "requester_email", "requester_phone",
               "requester_name", "full_name", "display_name", "matched_value")
SERVICE_ALIASES = {"profile_service": "account_service",
                   "support_replica": "support_platform"}


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('\\"', '"')
    return v


def _kv(text: str) -> dict:
    return {k: _unquote(v) for k, v in KV_RE.findall(text)}


def parse_line(line: str) -> dict | None:
    """Return {"ts", "service", "trace", "event", "fields"} or None."""
    m = TS_RE.match(line.strip())
    if not m:
        return None
    ts, rest = m.groups()
    service = trace = event = None
    fields: dict = {}
    if rest.startswith("{"):
        try:
            fields = {k: v for k, v in json.loads(rest).items()}
        except json.JSONDecodeError:
            return None
    elif (b := BRACKET_RE.match(rest)):
        _lvl, service, trace, event, tail = b.groups()
        fields = _kv(tail)
    elif (p := PIPE_RE.match(rest)):
        _lvl, service, trace, event, tail = p.groups()
        fields = _kv(tail)
    elif (s := SYSLOG_RE.match(rest)):
        _node, service, trace, tail = s.groups()
        fields = _kv(tail)
    else:
        fields = _kv(rest)
        if "message" in fields:          # logfmt with nested quoted logfmt
            fields.update(_kv(fields.pop("message")))
    service = fields.get("service") or fields.get("function") or fields.get("logger") or service
    trace = fields.get("trace_id") or fields.get("requestId") or trace
    event = fields.get("event") or event
    service = SERVICE_ALIASES.get(service, service)
    return {"ts": ts, "service": service, "trace": trace, "event": event,
            "fields": fields}


def _null(v) -> bool:
    return v is None or str(v).strip().lower() in {"", "null", "none"}


def _ids(fields: dict) -> set[str]:
    out = set()
    for k in ID_KEYS:
        n = normalize_identifier(fields.get(k))
        if n:
            out.add(n)
    return out


def outcome(service: str | None, event: str | None, f: dict) -> tuple[str, str, str] | None:
    """Map one outcome line to (system_id, category, state), or None if the
    line does not establish the state of a resource."""
    rid = f.get("resource_id")
    if not rid or not event:
        return None
    cat = category_for_resource(rid)
    ev = event.lower()

    # Explicit check rows embedded in logs.
    if "record_type" in f and "result" in f and f["result"].upper() in RESULT_STATES:
        cat = f["record_type"].upper()
        return f.get("system") or SYSTEM_FOR_CATEGORY.get(cat, service), cat, f["result"].upper()

    system = SYSTEM_FOR_CATEGORY.get(cat or "", service)
    if ev.endswith("_started"):
        return None

    if cat == "USER_PROFILE" and "http_status" in f:
        found = f["http_status"] == "200" and f.get("rows", "1") != "0"
        return system, cat, FOUND if found else NOT_FOUND
    if cat == "SEARCH_INDEX_ENTRY" and ("result" in f or "total_hits" in f):
        hit = f.get("result", "").lower() == "hit" or f.get("total_hits", "0") not in ("0", "")
        return system, cat, FOUND if hit else NOT_FOUND
    if cat == "DATA_FLOW" and "enabled" in f:
        return system, cat, ACTIVE if f["enabled"].lower() in ("true", "1") else DISABLED
    if cat == "BACKUP_ARCHIVE" and "eligible" in f:
        restorable = f["eligible"].lower() == "true" and f.get("deletion_block", "false").lower() != "true"
        return system, cat, ACTIVE if restorable else BLOCKED
    if cat == "BACKUP_ARCHIVE" and "decision" in f:
        return system, cat, BLOCKED if f["decision"].lower() in ("deny", "denied", "blocked") else ACTIVE
    if cat == "BACKUP_ARCHIVE" and "restore_allowed" in f:
        return system, cat, ACTIVE if f["restore_allowed"].lower() in ("true", "1") else BLOCKED
    if "contact" in ev and "email_opt_in" in f:
        # Marketing contact lookups (opt-in flags) are marketing profiles.
        return SYSTEM_FOR_CATEGORY.get(cat or "", service), "MARKETING_PROFILE", \
            FOUND if f.get("rows", "1") != "0" else NOT_FOUND
    if cat == "ANALYTICS_PROFILE" and "direct_identifiers" in f:
        return system, cat, IDENTIFIABLE if f["direct_identifiers"] not in ("0", "") else ANONYMIZED
    if cat == "ANALYTICS_PROFILE" and any(k in f for k in ("email", "customer_reference", "email_hash")):
        exposed = any(not _null(f.get(k)) for k in ("customer_reference", "email", "phone", "email_hash", "user_id"))
        return system, cat, IDENTIFIABLE if exposed else ANONYMIZED
    if cat == "SECURITY_AUDIT_LOG" and f.get("rows", "1") != "0":
        return system, cat, FOUND
    if rid.startswith("support_"):
        if "attachment" in ev:
            cat = "SUPPORT_ATTACHMENT"
            if f.get("rows") == "0":
                return "support_platform", cat, NOT_FOUND
            return "support_platform", cat, FOUND
        if "contact" in ev:
            # Legacy contact-index entries are secondary identifier copies.
            if "deletion_marker" in ev:
                return None
            return "support_platform", "SECONDARY_IDENTIFIER", \
                FOUND if f.get("rows", "1") != "0" else NOT_FOUND
        cat = "SUPPORT_MESSAGE"
        if "message_text" in f:
            direct = {i for i in extract_identifiers(f["message_text"]) if not i.startswith("user_")}
            return "support_platform", cat, IDENTIFIABLE if direct else REDACTED
        if "pii" in ev or "matched_value" in f:
            return "support_platform", cat, IDENTIFIABLE
        if f.get("rows") == "0":
            return "support_platform", cat, NOT_FOUND
        if any(k in f for k in ("requester_email", "requester_phone", "requester_name")):
            exposed = any(not _null(f.get(k)) for k in ("requester_email", "requester_phone", "requester_name"))
            return "support_platform", cat, IDENTIFIABLE if exposed else REDACTED
        if "rows" in f:
            return "support_platform", cat, FOUND
        return None
    if cat == "USER_EXPORT":
        if f.get("rows") == "0" or f.get("http_status") == "404":
            return system, cat, NOT_FOUND
        return system, cat, FOUND
    if "rows" in f:
        return system, cat, FOUND if f["rows"] != "0" else NOT_FOUND
    return None


def parse_log(ev: EvidenceFile) -> list[Observation]:
    lines = []
    for lineno, raw in enumerate(ev.path.read_text(encoding="utf-8").splitlines(), 1):
        p = parse_line(raw)
        if p:
            p["lineno"] = lineno
            p["raw"] = raw.strip()
            lines.append(p)

    trace_ids: dict[str, set[str]] = {}
    for p in lines:
        if p["trace"]:
            trace_ids.setdefault(p["trace"], set()).update(_ids(p["fields"]))

    obs = []
    for p in lines:
        f = p["fields"]
        res = outcome(p["service"], p["event"], f)
        if res is None:
            continue
        system, cat, state = res
        ids = _ids(f) | (trace_ids.get(p["trace"], set()) if p["trace"] else set())
        if "details" in f:
            ids |= extract_identifiers(f["details"])
        obs.append(Observation(
            resource_id=f["resource_id"], system_id=system, category=cat,
            state=state, identifiers=ids, observed_at=p["ts"],
            evidence_id=ev.file_id, source_kind="log",
            raw={"event": p["event"], "service": p["service"], "trace": p["trace"],
                 **{k: v for k, v in f.items() if isinstance(v, str)}},
            captured_at=ev.captured_at, location=f"{ev.rel_path}:{p['lineno']}"))
    return obs
