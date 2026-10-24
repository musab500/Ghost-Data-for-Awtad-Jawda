"""Shared interpretation of business records.

SQL ``record_json`` columns, SQL inventory tables, and JSON evidence files all
contain the same handful of record shapes (customer profiles, search documents,
analytics profiles, support tickets, connectors, backups, ...). This module
turns one such record into (system_id, category, state, identifiers) so the
format-specific parsers only have to locate records and timestamps.

State is derived from the record's *content*, never from workflow claims:
an analytics row with any non-null user_id/email/phone/hash is IDENTIFIABLE,
a connector with enabled=true is ACTIVE, a backup with restore_allowed=true is
ACTIVE, a tombstone that still carries a name/email/phone/hash is IDENTIFIABLE.
"""
from __future__ import annotations

from models import (ACTIVE, ANONYMIZED, BLOCKED, DISABLED, FOUND, IDENTIFIABLE,
                    NOT_FOUND, REDACTED, category_for_resource,
                    extract_identifiers, normalize_identifier)

# Explicit check results ("record_type"/"result" rows) may use these values.
RESULT_STATES = {"FOUND", "NOT_FOUND", "IDENTIFIABLE", "ANONYMIZED", "REDACTED",
                 "ACTIVE", "DISABLED", "BLOCKED"}

SYSTEM_FOR_CATEGORY = {
    "USER_PROFILE": "account_service",
    "SEARCH_INDEX_ENTRY": "search_service",
    "ANALYTICS_PROFILE": "analytics_platform",
    "SUPPORT_MESSAGE": "support_platform",
    "SUPPORT_ATTACHMENT": "support_platform",
    "USER_EXPORT": "export_service",
    "DATA_FLOW": "integration_service",
    "BACKUP_ARCHIVE": "backup_service",
    "SECURITY_AUDIT_LOG": "security_service",
    "LEGAL_HOLD_RECORD": "legal_service",
    "ACCOUNT_TOMBSTONE": "identity_service",
}

# Fields whose values are identifiers of the data subject.
ID_FIELDS = ("user_id", "customer_reference", "subject_reference", "email",
             "phone", "email_hash", "owner_email", "owner_phone",
             "requester_email", "requester_phone", "customer_email",
             "subject_selector", "uploaded_by", "lookup_value", "lookup_key",
             "query")
# Fields that are *direct* identifiers (must not survive redaction/anonymisation).
DIRECT_FIELDS = ("email", "phone", "email_hash", "name", "full_name",
                 "display_name", "owner_email", "owner_phone", "requester_email",
                 "requester_phone", "requester_name", "customer_email")
NAME_FIELDS = ("name", "full_name", "display_name", "requester_name")


def _present(v) -> bool:
    return v is not None and not (isinstance(v, str) and v.strip() == "")


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "enabled"}
    return bool(v)


def collect_identifiers(rec: dict) -> set[str]:
    ids: set[str] = set()
    for f in ID_FIELDS:
        n = normalize_identifier(rec.get(f))
        if n:
            ids.add(n)
    for f in ("message_text", "message_excerpt", "details"):
        if isinstance(rec.get(f), str):
            ids |= extract_identifiers(rec[f])
    return ids


def names_of(rec: dict) -> set[str]:
    return {str(rec[f]).strip() for f in NAME_FIELDS if _present(rec.get(f))}


def interpret(rec: dict, system_hint: str | None = None,
              collection_hint: str | None = None) -> dict | None:
    """Interpret one record. Returns a dict with keys
    resource_id, system_id, category, state, identifiers, names, links;
    or None if the record has no resource_id."""
    rid = rec.get("resource_id")
    if not rid:
        return None
    rid = str(rid).strip()
    coll = (collection_hint or "").lower()
    cat = category_for_resource(rid)
    ids = collect_identifiers(rec)
    names = names_of(rec)
    links: list[str] = []

    # 1. Explicit check rows: {"system", "record_type", "result", ...}
    if "record_type" in rec and "result" in rec:
        cat = str(rec["record_type"]).upper()
        state = str(rec["result"]).upper()
        if state not in RESULT_STATES:
            raise ValueError(f"unknown result {state!r} for {rid}")
        system = rec.get("system") or system_hint or SYSTEM_FOR_CATEGORY.get(cat)
        return _out(rid, system, cat, state, ids, names, links)

    # 2. Flatten nested profile / document payloads.
    if isinstance(rec.get("document"), dict):
        ids |= collect_identifiers(rec["document"])
        names |= names_of(rec["document"])
        direct_src = rec["document"]
    elif "profile" in rec and "status_code" in rec:
        prof = rec.get("profile")
        if rec.get("status_code") == 404 or prof is None:
            return _out(rid, system_hint or "account_service", "USER_PROFILE",
                        NOT_FOUND, ids, names, links)
        ids |= collect_identifiers(prof)
        names |= names_of(prof)
        return _out(rid, system_hint or "account_service", "USER_PROFILE",
                    FOUND, ids, names, links)
    elif isinstance(rec.get("hits"), dict):
        docs = rec["hits"].get("documents") or []
        mine = [d for d in docs if d.get("resource_id") in (None, rid)]
        if not mine or rec["hits"].get("total") == 0:
            return _out(rid, system_hint or "search_service", "SEARCH_INDEX_ENTRY",
                        NOT_FOUND, ids, names, links)
        for d in mine:
            ids |= collect_identifiers(d)
            names |= names_of(d)
        return _out(rid, system_hint or "search_service", "SEARCH_INDEX_ENTRY",
                    FOUND, ids, names, links)
    else:
        direct_src = rec

    if rec.get("source_resource_id"):
        links.append(str(rec["source_resource_id"]).strip())

    if rid.startswith("tombstone_") or "deleted_customer_references" in coll:
        cat = "ACCOUNT_TOMBSTONE"
        exposed = any(_present(direct_src.get(f)) for f in DIRECT_FIELDS)
        state = IDENTIFIABLE if exposed else REDACTED
    elif rid.startswith("support_"):
        if "file_name" in rec or "storage_class" in rec or "attachment" in coll:
            cat, state = "SUPPORT_ATTACHMENT", FOUND
        else:
            cat = "SUPPORT_MESSAGE"
            text_ids = extract_identifiers(str(rec.get("message_text") or "")
                                           + " " + str(rec.get("message_excerpt") or ""))
            text_direct = {i for i in text_ids if not i.startswith("user_")}
            exposed = bool(text_direct) or any(
                _present(rec.get(f)) for f in ("requester_email", "requester_phone",
                                               "requester_name", "email", "phone"))
            state = IDENTIFIABLE if exposed else REDACTED
    elif cat == "ANALYTICS_PROFILE":
        exposed = any(_present(rec.get(f)) for f in
                      ("user_id", "customer_reference", "email", "phone", "email_hash"))
        state = IDENTIFIABLE if exposed else ANONYMIZED
    elif cat == "DATA_FLOW":
        state = ACTIVE if _truthy(rec.get("enabled")) else DISABLED
    elif cat == "BACKUP_ARCHIVE":
        state = ACTIVE if _truthy(rec.get("restore_allowed")) else BLOCKED
    elif cat in ("USER_PROFILE", "SEARCH_INDEX_ENTRY", "USER_EXPORT",
                 "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
        state = FOUND
    else:
        # Unknown prefix: record exists; category left for downstream rules.
        state = FOUND

    system = system_hint or SYSTEM_FOR_CATEGORY.get(cat or "")
    return _out(rid, system, cat, state, ids, names, links)


def _out(rid, system, cat, state, ids, names, links) -> dict:
    return {"resource_id": rid, "system_id": system, "category": cat,
            "state": state, "identifiers": ids, "names": names, "links": links}
