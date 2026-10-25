"""Image and PDF evidence -> Observation objects.

PDFs
  Text is extracted locally (pdftotext, else pypdf) and interpreted with the
  same deterministic rules as the other parsers. A vision model is used only
  when a PDF has no extractable text.

PNG screenshots
  No OCR engine is installed, so screenshots are transcribed by a vision model
  through Person B's provider layer, llm_provider.vision_json (Gemini by default;
  this module never talks to a model SDK itself). One call per file, JSON schema
  with enums taken from models.STATES / models.CATEGORIES, and every record is
  validated again before use. Each file is looked up in this order:

    1. code/.cache/multimodal/   results of live calls (gitignored), keyed by
                                 sha256(file bytes + provider + model + PROMPT_VERSION)
    2. code/multimodal_vlm/      frozen, accepted results (committed, so code.zip
                                 reproduces the run with no key); matched by file
                                 hash + prompt version, whatever model made them
    3. a live call               if llm_provider has a configured provider; the
                                 result is cached under (1)
    4. code/multimodal_manual/   transcriptions made WITHOUT a model (method
                                 "manual_transcription", no usage): a documented
                                 fallback, used with a warning, and not at all when
                                 MULTIMODAL_ALLOW_MANUAL=0 (the final run)

  Re-runs make zero calls. Without a provider and without a cached result the file
  is skipped with a warning; nothing is invented. A failed call is a warning too:
  it never raises, because main.py's hook would turn an exception into "no
  multimodal evidence at all".

Every record must name its resource explicitly: either a resource id visible
in the evidence, or a business key (statement number, file name, customer
key) that the data catalog maps to a resource id (parsers.activity.
resource_aliases). Rows without either are dropped, never guessed.

Usage is written to code/.cache/multimodal_usage.json as a list ``calls`` in the
schema evaluation/usage_report.py reads (provider, model, input_tokens,
output_tokens, ok, usage_reported). ``calls`` lists the REAL calls behind the
vision results used by the run: calls made now (from_cache false, including
failed ones) and, for results served from a cache, the call that produced them
(from_cache true, original timestamp). Tokens come from the API response only;
nothing is estimated.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from models import (CATEGORIES, STATES, Observation, category_for_resource,
                    extract_identifiers, normalize_identifier, parse_ts)
from parsers.activity import RID_RE
from parsers.common import SYSTEM_FOR_CATEGORY
from parsers.manifest import EvidenceFile

CODE_DIR = Path(__file__).resolve().parent
CACHE_DIR = CODE_DIR / ".cache" / "multimodal"          # results of live calls (gitignored)
FROZEN_DIR = CODE_DIR / "multimodal_vlm"                # accepted results (committed)
MANUAL_DIR = CODE_DIR / "multimodal_manual"             # manual transcriptions (committed fallback)
USAGE_PATH = CODE_DIR / ".cache" / "multimodal_usage.json"
PROMPT_VERSION = "v5"
# Default of MULTIMODAL_ALLOW_MANUAL ("1" = fallback allowed, "0" = never). Change this
# to "0" once the accepted vision results are frozen in code/multimodal_vlm/ (see NOTES.md).
MANUAL_FALLBACK_DEFAULT = "1"
KNOWN_SYSTEMS = frozenset(SYSTEM_FOR_CATEGORY.values()) | {"marketing_platform", "object_storage"}
CALL_PAUSE_SECONDS = float(os.environ.get("MULTIMODAL_CALL_PAUSE", "13"))   # between live calls
RATE_LIMIT_BACKOFF = ()                          # extra waits after the provider's own retries (none: a 429 stops the run)
_sleep = time.sleep                              # replaced in tests
MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "pdf": "application/pdf"}

RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string"},
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "system_id": {"type": "string"},
                    "category": {"type": "string", "enum": sorted(CATEGORIES)},
                    "state": {"type": "string", "enum": sorted(STATES)},
                    "identifiers": {"type": "array", "items": {"type": "string"}},
                    "observed_at": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["resource_id", "keys", "system_id", "category", "state", "identifiers",
                             "observed_at", "notes"],
            },
        }
    },
    "required": ["records"],
}

SYSTEM_PROMPT = ("You transcribe screenshots of internal business tools for a personal-data deletion "
                 "audit. Return only the requested JSON. Transcribe what is visible; never guess.")

VISION_PROMPT = f"""List every record the screen shows that has its own reference (a resource id such as
account_123 / search_45 / backup_9 / data_flow_7 / export_12 / support_3 / audit_record_8 /
marketing_1 / analytics_5 / tombstone_2, a file name, a statement number, a ticket number, or a customer key).
For each record return:
- resource_id: the record's resource id if one is shown (e.g. export_1260), else ""
- keys: every other reference shown for that record (file name, statement number,
  ticket number, customer key), exactly as displayed
- system_id: the id of the internal service that owns the record, one of: account_service,
  search_service, analytics_platform, support_platform, export_service, integration_service,
  backup_service, security_service, identity_service, legal_service, marketing_platform,
  object_storage. Never a product, brand or application name shown on the screen; if unsure, ""
- category: one of {sorted(CATEGORIES)}. The prefix of a resource id names its category
  (account_ -> USER_PROFILE, search_ -> SEARCH_INDEX_ENTRY, backup_ -> BACKUP_ARCHIVE,
  tombstone_ -> ACCOUNT_TOMBSTONE, audit_record_ -> SECURITY_AUDIT_LOG, ...); follow it
- state, by what the screen shows for that record:
  * DATA_FLOW and BACKUP_ARCHIVE records take only ACTIVE (running / enabled / restorable),
    DISABLED (off, paused, stopped) or BLOCKED (restore blocked). An identifier shown in a
    selector or detail field does not make them IDENTIFIABLE
  * every other record: FOUND = the record is listed and no direct identifier of a person is
    displayed for it; NOT_FOUND = the screen says it does not exist; IDENTIFIABLE = the record's
    own row or detail panel displays a direct identifier (name, email, phone, email hash); a
    record that only links to a user reference (a user id) is FOUND, not IDENTIFIABLE
  * ANONYMIZED / REDACTED = kept, with the identifying fields shown as removed / masked. A
    retained reference or tombstone page that shows only a stable user reference and no direct
    identifier (no name, email, phone, hash) is REDACTED
- identifiers: every email, phone number, user_id and email hash displayed in that record's own
  row or detail panel. Do not copy an identifier shown elsewhere on the screen (another record,
  a header, a customer panel) onto a record that does not display it
- observed_at: ISO-8601 UTC time the screen says the data was captured or refreshed, or "" if none
- notes: one short factual sentence (at most 15 words)
Include every record the page shows or links to: rows of a list, rows of a table, the detail
panel, and records mentioned in an activity feed or in a field such as "Support" or "Linked
record", even when the record has no row of its own. Do not drop a row because another record
on the page looks more important.
Transcribe only what is visible. Do not infer records that are not shown."""


@dataclass
class VisionBackend:
    """What multimodal.py needs from a model provider. ``call`` has the signature of
    llm_provider.vision_json and returns (parsed_dict, usage); on failure it raises an
    exception that may carry ``.usage`` (tokens spent) and ``.kind``."""
    provider: str
    model: str                 # the model that will be requested (part of the cache key)
    call: Callable


def default_backend() -> VisionBackend | None:
    """The configured provider from llm_provider (Person B), or None when llm_provider
    is not importable or no key is configured."""
    try:
        import llm_provider as llm
    except ImportError:
        return None
    llm.load_dotenv()
    if not llm.is_configured():
        return None
    return VisionBackend(llm.provider_name(), llm.model_for("vision"), llm.vision_json)


def load_env() -> None:
    """Load the gitignored .env (keys and settings) through llm_provider, if present."""
    try:
        import llm_provider as llm
    except ImportError:
        return
    llm.load_dotenv()


# ------------------------------------------------------------------ entry
_AUTO = object()


def multimodal_observations(files: list[EvidenceFile],
                            aliases=None,
                            cache_dir: Path = CACHE_DIR,
                            log=sys.stderr,
                            manual_dir: Path = MANUAL_DIR,
                            usage_path: Path | None = None,
                            frozen_dir: Path = FROZEN_DIR,
                            backend=_AUTO) -> list[Observation]:
    """Observations for the image/PDF evidence files skipped by load_evidence.

    aliases: the catalog alias map (parsers.activity.resource_aliases), or the
    dataset directory, from which it is built (the main.py hook passes that).
    backend: a VisionBackend, None (no model), or omitted: llm_provider when MULTIMODAL_LIVE=1, else no model."""
    load_env()
    if isinstance(aliases, (str, Path)):
        from parsers.activity import parse_activity, resource_aliases
        aliases = resource_aliases(parse_activity(Path(aliases))[0])
    aliases = aliases or {}
    if backend is _AUTO:       # a live vision call needs an explicit opt-in (MULTIMODAL_LIVE=1) or --run
        backend = default_backend() if os.environ.get("MULTIMODAL_LIVE") == "1" else None
    usage_path = usage_path or (USAGE_PATH if cache_dir == CACHE_DIR else cache_dir / "usage.json")
    cache_dir.mkdir(parents=True, exist_ok=True)
    allow_manual = os.environ.get("MULTIMODAL_ALLOW_MANUAL", MANUAL_FALLBACK_DEFAULT) != "0"
    fresh: list[dict] = []        # calls made in this run (successful or not)
    used: list[dict] = []         # how each file was satisfied
    skipped: list[dict] = []
    out: list[Observation] = []
    n_live, rate_limited = 0, False
    for ev in sorted(files, key=lambda e: e.file_id):
        if ev.kind == "pdf":
            text = pdf_text(ev.path)
            if text.strip():
                out += _pdf_observations(ev, text, aliases)
                used.append({"file_id": ev.file_id, "method": "pdf_text"})
                continue
        if ev.kind not in MIME:
            skipped.append({"file_id": ev.file_id, "reason": f"unsupported type {ev.kind}"})
            continue
        entry, from_cache = _cached_vlm_entry(ev, backend, cache_dir, frozen_dir), True
        if entry is None and backend is not None and not rate_limited:
            if n_live:
                _sleep(CALL_PAUSE_SECONDS)
            n_live += 1
            entry, from_cache = _call_vision(ev, backend, cache_dir, fresh, log), False
            for wait in RATE_LIMIT_BACKOFF:
                if entry is not None or not fresh or fresh[-1].get("error_kind") != "rate_limit":
                    break
                print(f"multimodal: {ev.file_id}: rate limited ({_quota_kind(_LAST_ERROR[0])}); "
                      f"waiting {wait:.0f}s before retrying", file=log)
                _sleep(wait)
                entry = _call_vision(ev, backend, cache_dir, fresh, log)
            if entry is None and fresh:
                rate_limited = True      # any failed live call stops the run: no more calls on a failing setup
                print(f"multimodal: STOP: live call failed for {ev.file_id} [{fresh[-1].get('error_kind')}]"
                      + (f" ({_quota_kind(_LAST_ERROR[0])})" if fresh[-1].get("error_kind") == "rate_limit" else "")
                      + "; no further live calls in this run", file=log)
        if entry is None and allow_manual:
            entry = _manual_entry(ev, manual_dir)
            if entry is not None:
                print(f"multimodal: WARNING {ev.file_id} ({ev.rel_path}): using the manual "
                      "transcription (no vision-model result available)", file=log)
        if entry is None:
            why = "no vision result and no provider configured" if backend is None else "no usable vision result"
            skipped.append({"file_id": ev.file_id, "reason": why})
            print(f"multimodal: WARNING {ev.file_id} ({ev.rel_path}) not processed: {why}", file=log)
            continue
        x = entry["extractor"]
        used.append({"file_id": ev.file_id, "method": x["method"], "provider": x.get("provider"),
                     "model": x.get("served_model") or x.get("model"), "from_cache": from_cache,
                     "usage": entry.get("usage")})
        out += records_to_observations(ev, entry["records"], aliases, log, entry)
    if files:   # a run with no image/PDF input must not overwrite the last real report
        _write_usage(usage_path, fresh, used, skipped)
    return out


# ------------------------------------------------------------------ PDFs
def pdf_text(path: Path) -> str:
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                           text=True, check=False)
        if r.returncode == 0:
            return r.stdout
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


RETAIN_RE = re.compile(r"retain until\s+(\d{1,2}) (\w{3}) (\d{4})\s+(\d{2})\s+(\d{2})", re.I)


def _pdf_observations(ev: EvidenceFile, text: str,
                      aliases: dict[str, tuple[str, str]]) -> list[Observation]:
    """Deterministic interpretation of a text PDF: one Observation per resource
    named in the document (explicit id or catalog alias). Customer keys
    (user_N) are never used as aliases in free text: a document that merely
    mentions a user is not evidence about that user's tombstone."""
    low = text.lower()
    found: dict[str, str | None] = {}
    for rid in RID_RE.findall(text):
        found.setdefault(rid, None)
    for alias, (rid, system) in aliases.items():
        if re.fullmatch(r"user_\d+", alias):
            continue
        if alias in low:
            found.setdefault(rid, system)
    raw = {}
    m = RETAIN_RE.search(text)
    if m:
        d, mon, y, hh, mm = m.groups()
        raw["retention_until"] = datetime.strptime(f"{d} {mon} {y} {hh}:{mm}", "%d %b %Y %H:%M") \
            .replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    obs = []
    for rid, system in sorted(found.items()):
        if len(found) == 1:
            ids = extract_identifiers(text)
        else:   # several records: only identifiers on the lines naming this record
            ids = set().union(*(extract_identifiers(l) for l in text.splitlines() if rid in l))
        cat = category_for_resource(rid)
        if rid.startswith(("support_", "object_")) and "attachment" in low:
            cat = "SUPPORT_ATTACHMENT"
        system = system or SYSTEM_FOR_CATEGORY.get(cat or "", "")
        obs.append(Observation(
            resource_id=rid, system_id=system, category=cat, state="FOUND",
            identifiers=ids, observed_at=ev.captured_at, evidence_id=ev.file_id,
            source_kind="pdf", raw={**raw, "extractor": "pdf_text", "method": "pdf_text"},
            captured_at=ev.captured_at, location=ev.rel_path))
    return obs


# ------------------------------------------------------------------ vision
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cache_key(path: Path, provider: str, model: str) -> str:
    """sha256(file bytes + provider + model + prompt version)."""
    h = hashlib.sha256(path.read_bytes())
    h.update(f"\x00{provider}\x00{model}\x00{PROMPT_VERSION}".encode())
    return h.hexdigest()


def _is_vlm_entry(e: dict, sha: str) -> bool:
    x = e.get("extractor", {})
    return (e.get("file_sha256") == sha and x.get("method") == "vlm"
            and x.get("prompt_version") == PROMPT_VERSION and isinstance(e.get("records"), list))


def _scan(directory: Path | None, sha: str) -> list[dict]:
    """All vision results for this file's bytes in a directory, in file-name order."""
    if directory is None or not directory.is_dir():
        return []
    found = []
    for p in sorted(directory.glob("*.json")):
        try:
            e = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(e, dict) and _is_vlm_entry(e, sha):
            found.append(e)
    return found


def _cached_vlm_entry(ev: EvidenceFile, backend: VisionBackend | None, cache_dir: Path,
                      frozen_dir: Path | None) -> dict | None:
    """A vision result for this file without a live call.

    With a provider configured, the live-call cache is matched exactly on
    (provider, model); frozen results are accepted whatever model produced them
    (so a judge's run reproduces the submitted output), preferring a match.
    Without a provider, any cached or frozen result for these bytes is used."""
    sha = _sha256(ev.path)
    if backend is not None:
        p = cache_dir / f"{_cache_key(ev.path, backend.provider, backend.model)}.json"
        if p.exists():
            e = json.loads(p.read_text(encoding="utf-8"))
            if _is_vlm_entry(e, sha):
                return e
        frozen = _scan(frozen_dir, sha)
        same = [e for e in frozen if (e["extractor"].get("provider"), e["extractor"].get("model"))
                == (backend.provider, backend.model)]
        return (same or frozen or [None])[0]
    return (_scan(frozen_dir, sha) + _scan(cache_dir, sha) or [None])[0]


def _manual_entry(ev: EvidenceFile, manual_dir: Path) -> dict | None:
    """A committed manual transcription of the same file bytes (fallback only)."""
    if not manual_dir.exists():
        return None
    sha = _sha256(ev.path)
    for m in sorted(manual_dir.glob(f"{sha[:16]}__*.json")):
        entry = json.loads(m.read_text(encoding="utf-8"))
        if entry.get("file_sha256") == sha and \
                entry.get("extractor", {}).get("method") == "manual_transcription":
            return entry
    return None


def _response_records(file_id: str, usage: dict | None, ok: bool, from_cache: bool, at,
                      default_provider: str = None, default_model: str = None,
                      error_kind: str | None = None) -> list[dict]:
    """Ledger records for one provider call: ONE record per API response.

    The provider reports ``usage["attempts"]`` (one entry per response, e.g. a
    truncated answer and its retry, each billed). Without it (older results, a
    failure that carried no response) the call is a single record. Tokens are as
    reported; a call with no usage is recorded with 0 tokens, usage_reported false."""
    usage = usage or {}
    attempts = usage.get("attempts")
    if not attempts:
        rec = {"file_id": file_id, "provider": usage.get("provider", default_provider),
               "model": usage.get("model") or default_model,
               "input_tokens": int(usage.get("input_tokens", 0) or 0),
               "output_tokens": int(usage.get("output_tokens", 0) or 0), "ok": ok,
               "usage_reported": bool(usage.get("usage_reported", ok)), "from_cache": from_cache, "at": at}
        if not ok:
            rec["error_kind"] = error_kind
        return [rec]
    out = []
    for a in attempts:
        rec = {"file_id": file_id, "provider": a.get("provider") or default_provider,
               "model": a.get("model") or default_model,
               "input_tokens": int(a.get("input_tokens", 0) or 0), "output_tokens": int(a.get("output_tokens", 0) or 0),
               "ok": bool(a.get("ok")), "usage_reported": bool(a.get("usage_reported", True)),
               "retry": bool(a.get("retry", False)), "max_output_tokens": a.get("max_output_tokens"),
               "from_cache": from_cache, "at": at}
        if not rec["ok"]:
            rec["error_kind"] = a.get("failure") or error_kind
        out.append(rec)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_LAST_ERROR = [""]


def _quota_kind(message: str) -> str:
    """Per-minute vs per-day, only when the provider's message says so."""
    m = (message or "").lower().replace(" ", "")
    if "perday" in m or "daily" in m:
        return "per-day quota"
    if "perminute" in m:
        return "per-minute quota"
    return "quota window not stated in the message"


def _call_vision(ev: EvidenceFile, backend: VisionBackend, cache_dir: Path, fresh: list, log) -> dict | None:
    """One vision call for one file. Returns the cache entry, or None (with a
    warning) on any failure: this function never raises. Every attempt is
    recorded in ``fresh`` with the tokens the API reported (none are estimated)."""
    try:
        parsed, usage = backend.call(SYSTEM_PROMPT, VISION_PROMPT, ev.path.read_bytes(),
                                     MIME[ev.kind], RECORD_SCHEMA)
        records = parsed["records"]
        if not isinstance(records, list):
            raise ValueError("'records' is not a list")
    except Exception as e:     # noqa: BLE001 - see docstring; main.py's hook swallows exceptions
        kind = getattr(e, "kind", type(e).__name__)
        fresh += _response_records(ev.file_id, getattr(e, "usage", None), False, False, _now(),
                                   backend.provider, backend.model, kind)
        _LAST_ERROR[0] = str(e)
        print(f"multimodal: WARNING {ev.file_id}: vision call failed [{kind}]: {str(e)[:600]}", file=log)
        return None
    at = _now()
    fresh += _response_records(ev.file_id, usage, True, False, at, backend.provider, backend.model)
    served = usage.get("model") or backend.model
    entry = {"file_id": ev.file_id, "file_path": ev.rel_path, "file_sha256": _sha256(ev.path),
             "extractor": {"method": "vlm", "provider": backend.provider, "model": backend.model,
                           "served_model": served, "prompt_version": PROMPT_VERSION},
             "usage": {**usage, "at": at}, "records": records}
    path = cache_dir / f"{_cache_key(ev.path, backend.provider, backend.model)}.json"
    path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return entry


# ------------------------------------------------------------------ validation
def records_to_observations(ev: EvidenceFile, records: list[dict],
                            aliases: dict[str, tuple[str, str]], log=sys.stderr,
                            entry: dict | None = None) -> list[Observation]:
    """Validate transcribed records and convert them. Records whose state or
    category is outside the allowed vocabulary, or whose resource cannot be
    named explicitly, are dropped with a warning. ``entry`` is the cache entry the
    records came from; it supplies raw["method"] for the evaluation report."""
    x = (entry or {}).get("extractor", {})
    if x.get("method") == "vlm":
        method = f"vlm:{x.get('provider')}/{x.get('served_model') or x.get('model')}"
    else:
        method = x.get("method") or "vlm"
    out = []
    for i, r in enumerate(records):
        rid, system = _resolve(([r["resource_id"]] if r.get("resource_id") else []) + list(r.get("keys") or []),
                               aliases)
        state = str(r.get("state", "")).upper()
        cat = str(r.get("category", "")).upper() or None
        problem = None
        if rid is None:
            problem = f"no resource id or catalog alias in keys {r.get('keys')}"
        elif state not in STATES:
            problem = f"invalid state {state!r}"
        elif cat is not None and cat not in CATEGORIES:
            problem = f"invalid category {cat!r}"
        if problem:
            print(f"multimodal: {ev.file_id} record {i} dropped: {problem}", file=log)
            continue
        by_id = category_for_resource(rid)
        if by_id and cat != by_id:                 # the id prefix names the category; the id wins
            print(f"multimodal: {ev.file_id} {rid}: category {cat} -> {by_id} (from id prefix)", file=log)
            cat = by_id
        ids = {n for n in (normalize_identifier(x) for x in r.get("identifiers") or []) if n}
        observed = ev.captured_at
        t = r.get("observed_at") or ""
        try:
            if t and parse_ts(t) <= parse_ts(ev.captured_at):
                observed = t
        except ValueError:
            pass
        said = (r.get("system_id") or "").strip()
        if said not in KNOWN_SYSTEMS:              # a product/brand name is not a system id
            said = ""
        # the catalog names the owning system of a category (as the parsers do); the model's guess is
        # used only for categories the catalog does not map
        system = system or SYSTEM_FOR_CATEGORY.get(cat or "", "") or said
        out.append(Observation(
            resource_id=rid, system_id=system, category=cat, state=state, identifiers=ids,
            observed_at=observed, evidence_id=ev.file_id, source_kind="image",
            raw={"keys": r.get("keys"), "notes": r.get("notes", ""), "extractor": "vision", "method": method},
            captured_at=ev.captured_at, location=ev.rel_path))
    return out


def _resolve(keys: list[str], aliases: dict[str, tuple[str, str]]) -> tuple[str | None, str | None]:
    for k in keys:
        m = RID_RE.fullmatch(k.strip())
        if m:
            return k.strip(), None
    for k in keys:
        hit = aliases.get(k.strip().lower())
        if hit:
            return hit
    return None, None


PRIOR_RUNS_PATH = CODE_DIR / "multimodal_usage_prior_runs.json"   # superseded runs, committed


def _prior_run_calls() -> list:
    """Real calls of earlier runs whose results were superseded (still spent, still reported)."""
    try:
        return list(json.loads(PRIOR_RUNS_PATH.read_text(encoding="utf-8")).get("calls", []))
    except (OSError, ValueError):
        return []


def _write_usage(path: Path, fresh: list, used: list, skipped: list) -> None:
    """Write the usage ledger (schema read by evaluation/usage_report.py).

    ``calls``: one record per API response behind the vision results used by this
    run. Failed responses of this run come first (including those of files that
    then fell back or were skipped); then, per vision-derived file, the responses
    that produced its result (from_cache true if that was an earlier run)."""
    calls = [c for c in fresh if not c["ok"]]
    for u in used:
        if u["method"] != "vlm" or not u.get("usage"):
            continue
        if u["from_cache"]:
            us = u["usage"]
            recs = _response_records(u["file_id"], us, True, True, us.get("at"), u.get("provider"), u.get("model"))
            calls += recs          # includes a failed first attempt, which was billed too
        else:
            calls += [c for c in fresh if c["ok"] and c["file_id"] == u["file_id"]]
    calls += _prior_run_calls()
    report = {
        "calls": calls,
        "api_calls_this_run": len(fresh),            # API responses, retries counted
        "input_tokens_this_run": sum(c["input_tokens"] for c in fresh),
        "output_tokens_this_run": sum(c["output_tokens"] for c in fresh),
        "files": used,
        "files_skipped": skipped,
        "run_at": _now(),
        "note": "calls = the real calls behind the vision results used by this run; from_cache=true "
                "ones were made in an earlier run. Tokens are as reported by the API; none are estimated. "
                "PDF text and manual transcriptions involve no model call.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
