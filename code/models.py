"""Shared data model for the deletion-audit pipeline.

Every parser turns its evidence format into ``Observation`` objects: one
statement of the form "at time T, source E saw resource R in system S in
state X, linked to identifiers I". Downstream code (state reconstruction,
rules, step generation) only ever sees Observations, never raw files.

Identifier normalisation is defined here so that parsers and identity
resolution produce comparable tokens:

* emails  -> lower-cased, stripped               ("Ann@X.com" -> "ann@x.com")
* phones  -> digits only, prefixed with "+"      ("+34 612-84" -> "+3461284")
* user ids / resource ids -> lower-cased          ("USER_32" -> "user_32")
* sha256 email hashes -> lower-cased hex
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Canonical states an Observation can carry.
FOUND = "FOUND"                # record exists (deletable categories)
NOT_FOUND = "NOT_FOUND"        # record confirmed absent
IDENTIFIABLE = "IDENTIFIABLE"  # record exists and still exposes direct identifiers
ANONYMIZED = "ANONYMIZED"      # record exists, unlinkable from the user
REDACTED = "REDACTED"          # record exists, direct identifiers removed
ACTIVE = "ACTIVE"              # data flow enabled / restore allowed
DISABLED = "DISABLED"          # data flow disabled
BLOCKED = "BLOCKED"            # backup restore blocked
RETAINED = "RETAINED"          # policy-retained record present (reserved)

STATES = {FOUND, NOT_FOUND, IDENTIFIABLE, ANONYMIZED, REDACTED, ACTIVE,
          DISABLED, BLOCKED, RETAINED}

# Data categories: the deletion_rules.csv categories plus DATA_FLOW.
CATEGORIES = {
    "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY",
    "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE",
    "PRODUCT_TELEMETRY", "USER_EXPORT", "MARKETING_PROFILE",
    "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD",
    "BACKUP_ARCHIVE", "ACCOUNT_TOMBSTONE", "DATA_FLOW",
}


@dataclass
class Observation:
    resource_id: str
    system_id: str
    category: str | None
    state: str
    identifiers: set[str]
    observed_at: str          # ISO-8601 UTC time the state was observed
    evidence_id: str          # file_id (or activity_id / audit_id) to cite
    source_kind: str          # sql | json | log | txt | activity | image | pdf
    raw: dict = field(default_factory=dict)
    # Manifest capture time of the evidence file. An observation may only be
    # used (and cited) for a case when captured_at <= audit_at.
    captured_at: str = ""
    # Where inside the evidence the fact came from (table, JSON path, line).
    location: str = ""

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown state {self.state!r} for {self.resource_id}")
        if self.category is not None and self.category not in CATEGORIES:
            raise ValueError(f"unknown category {self.category!r} for {self.resource_id}")


@dataclass
class Scope:
    """A complete point-in-time capture of one system (or one replica table).

    Any resource of ``system_id`` that is *not* in ``present`` was absent from
    this copy at ``observed_at``. This is how snapshots prove deletion: e.g. an
    account table export that no longer lists account_001.
    """
    evidence_id: str
    system_id: str
    observed_at: str
    present: set[str]
    copy: str = ""            # table / collection name, for replica drift
    captured_at: str = ""     # manifest capture time of the evidence file
    source_kind: str = ""


@dataclass
class ResourceLink:
    """An explicit statement that a resource belongs to a person (e.g. a data
    catalog LINK_RESOURCE_IDENTITY activity). Carries no state."""
    resource_id: str
    system_id: str
    identifiers: set[str]
    observed_at: str
    evidence_id: str
    exclusive: bool = False   # "belongs only to" this subject


@dataclass
class ActivityEvent:
    """One system_activity.csv row. reported_result is a *claim*, not proof."""
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str
    # Other resource ids mentioned in details (e.g. the search entry a
    # restore job repopulated).
    related_resources: list[str] = field(default_factory=list)


@dataclass
class Identity:
    user_id: str
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    email_hashes: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)

    def tokens(self) -> set[str]:
        """All normalised identifier tokens that can link evidence to this user."""
        return {norm_id(self.user_id)} | self.emails | self.phones | self.email_hashes


# ---------------------------------------------------------------- normalisers

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# International phone numbers as written in the dataset: "+<country> ...".
PHONE_RE = re.compile(r"\+\d[\d\s\-().]{6,}\d")
HASH_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
USER_RE = re.compile(r"\buser_\d+\b", re.IGNORECASE)


def norm_email(value: str) -> str:
    return value.strip().lower()


def norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return "+" + digits if digits else ""


def norm_id(value: str) -> str:
    return value.strip().lower()


def normalize_identifier(value) -> str | None:
    """Normalise a single identifier-like value; returns None for empty/null."""
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s or s.lower() in {"null", "none", "no direct identifier"}:
        return None
    if "@" in s:
        return norm_email(s)
    if HASH_RE.fullmatch(s):
        return s.lower()
    if s.startswith("+") and PHONE_RE.fullmatch(s):
        return norm_phone(s)
    return norm_id(s)


def extract_identifiers(text: str) -> set[str]:
    """Pull emails, phones, hashes and user ids out of free text."""
    if not text:
        return set()
    out = {norm_email(m) for m in EMAIL_RE.findall(text)}
    out |= {norm_phone(m) for m in PHONE_RE.findall(text)}
    out |= {m.lower() for m in HASH_RE.findall(text)}
    out |= {m.lower() for m in USER_RE.findall(text)}
    return out


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def category_for_resource(resource_id: str) -> str | None:
    """Fallback category from the resource-id prefix (used when a source gives none)."""
    prefix = resource_id.rsplit("_", 1)[0].lower()
    return {
        "account": "USER_PROFILE",
        "search": "SEARCH_INDEX_ENTRY",
        "analytics": "ANALYTICS_PROFILE",
        "export": "USER_EXPORT",
        "data_flow": "DATA_FLOW",
        "backup": "BACKUP_ARCHIVE",
        "audit_record": "SECURITY_AUDIT_LOG",
        "legal_hold": "LEGAL_HOLD_RECORD",
        "tombstone": "ACCOUNT_TOMBSTONE",
        "cache": "CACHE_ENTRY",
        "telemetry": "PRODUCT_TELEMETRY",
        "marketing": "MARKETING_PROFILE",
    }.get(prefix)
