"""Image and PDF evidence -> Observation objects.

PDFs
  Text is extracted locally (pdftotext, else pypdf) and interpreted with the
  same deterministic rules as the other parsers. A vision model is used only
  when a PDF has no extractable text.

PNG screenshots
  No OCR engine is installed, so screenshots are transcribed by a vision LLM:
  one call per file, structured JSON output, validated before use. Results are
  cached in code/multimodal_cache/ keyed by file SHA-256 + model + prompt
  version, so a full run calls the model at most once per file, ever.

  The API key is read from ANTHROPIC_API_KEY only. Without a key, a file with
  no usable cache entry is skipped with a warning; nothing is invented.
  Cache entries record how they were produced ("api" with real token usage, or
  "manual_transcription" for entries transcribed outside the API, with usage
  null). Set MULTIMODAL_ALLOW_MANUAL=0 to ignore manual entries.

Every record must name its resource explicitly: either a resource id visible
in the evidence, or a business key (statement number, file name, customer
key) that the data catalog maps to a resource id (parsers.activity.
resource_aliases). Rows without either are dropped, never guessed.

API usage for the run is written to code/multimodal_cache/usage.json.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from models import (CATEGORIES, Observation, category_for_resource, extract_identifiers,
                    normalize_identifier, parse_ts)
from parsers.activity import RID_RE
from parsers.common import RESULT_STATES, SYSTEM_FOR_CATEGORY
from parsers.manifest import EvidenceFile

CACHE_DIR = Path(__file__).resolve().parent / "multimodal_cache"
PROMPT_VERSION = "v1"
DEFAULT_MODEL = "claude-opus-5-5"

RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "system_id": {"type": "string"},
                    "category": {"type": "string"},
                    "state": {"type": "string"},
                    "identifiers": {"type": "array", "items": {"type": "string"}},
                    "observed_at": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["keys", "system_id", "category", "state", "identifiers",
                             "observed_at", "notes"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["records"],
    "additionalProperties": False,
}

VISION_PROMPT = f"""You are transcribing an internal business screenshot for a data-deletion audit.
List every record the screen shows that has its own reference (a resource id such as
account_123 / search_45 / backup_9 / data_flow_7 / export_12 / support_3 / audit_record_8 /
marketing_1 / analytics_5, a file name, a statement number, or a customer key).
For each record return:
- keys: every reference shown for that record, exactly as displayed
- system_id: the owning system (e.g. account_service, search_service, analytics_platform,
  support_platform, export_service, integration_service, backup_service, security_service,
  identity_service, marketing_platform, object_storage)
- category: one of {sorted(CATEGORIES)}
- state: FOUND (record exists), NOT_FOUND, IDENTIFIABLE (record shows a direct identifier:
  name, email, phone, email hash), ANONYMIZED, REDACTED (kept but direct identifiers removed),
  ACTIVE (data flow running / backup restorable), DISABLED (data flow off), BLOCKED (restore blocked)
- identifiers: every email, phone number, user_id and email hash shown for that record
- observed_at: ISO-8601 UTC time the screen says the data was captured or refreshed, or "" if none
- notes: one short factual sentence
Transcribe only what is visible. Do not infer records that are not shown."""


# ------------------------------------------------------------------ entry
def multimodal_observations(files: list[EvidenceFile],
                            aliases: dict[str, tuple[str, str]] | None = None,
                            cache_dir: Path = CACHE_DIR,
                            log=sys.stderr) -> list[Observation]:
    """Observations for the image/PDF evidence files skipped by load_evidence."""
    aliases = aliases or {}
    cache_dir.mkdir(parents=True, exist_ok=True)
    usage_calls: list[dict] = []
    cached: list[dict] = []
    skipped: list[dict] = []
    out: list[Observation] = []
    for ev in sorted(files, key=lambda e: e.file_id):
        if ev.kind == "pdf":
            text = pdf_text(ev.path)
            if text.strip():
                out += _pdf_observations(ev, text, aliases)
                cached.append({"file_id": ev.file_id, "method": "pdf_text"})
                continue
        if ev.kind not in ("png", "jpg", "jpeg", "pdf"):
            skipped.append({"file_id": ev.file_id, "reason": f"unsupported type {ev.kind}"})
            continue
        entry = _cached_entry(ev, cache_dir)
        if entry is None:
            entry = _call_vision(ev, cache_dir, usage_calls, log)
        if entry is None:
            skipped.append({"file_id": ev.file_id, "reason": "no cache entry and no API key"})
            print(f"multimodal: WARNING {ev.file_id} ({ev.rel_path}) not processed: "
                  "no cache entry and ANTHROPIC_API_KEY is not set", file=log)
            continue
        cached.append({"file_id": ev.file_id, "method": entry["extractor"]["method"],
                       "model": entry["extractor"].get("model"),
                       "usage": entry.get("usage")})
        out += records_to_observations(ev, entry["records"], aliases, log)
    if files:   # a run with no image/PDF input must not overwrite the last real report
        _write_usage(cache_dir, usage_calls, cached, skipped)
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
            source_kind="pdf", raw={**raw, "extractor": "pdf_text"},
            captured_at=ev.captured_at, location=ev.rel_path))
    return obs


# ------------------------------------------------------------------ vision
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _model() -> str:
    return os.environ.get("MULTIMODAL_MODEL", DEFAULT_MODEL)


def _cached_entry(ev: EvidenceFile, cache_dir: Path) -> dict | None:
    """API entry for the current model + prompt version, else (if allowed) a
    manual transcription of the same file bytes."""
    sha = _sha256(ev.path)
    allow_manual = os.environ.get("MULTIMODAL_ALLOW_MANUAL", "1") != "0"
    candidates = []
    for p in sorted(cache_dir.glob(f"{sha[:16]}__*.json")):
        entry = json.loads(p.read_text(encoding="utf-8"))
        if entry.get("file_sha256") != sha:
            continue
        x = entry.get("extractor", {})
        if x.get("method") == "api" and x.get("model") == _model() \
                and x.get("prompt_version") == PROMPT_VERSION:
            return entry
        if x.get("method") == "manual_transcription" and allow_manual:
            candidates.append(entry)
    return candidates[0] if candidates else None


def _call_vision(ev: EvidenceFile, cache_dir: Path, usage_calls: list, log) -> dict | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic   # only needed when a file is not cached

    client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY
    media = "application/pdf" if ev.kind == "pdf" else f"image/{'jpeg' if ev.kind == 'jpg' else ev.kind}"
    block_type = "document" if ev.kind == "pdf" else "image"
    data = base64.standard_b64encode(ev.path.read_bytes()).decode("utf-8")
    model = _model()
    kwargs = dict(
        model=model, max_tokens=16000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": RECORD_SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": block_type, "source": {"type": "base64", "media_type": media, "data": data}},
            {"type": "text", "text": VISION_PROMPT}]}],
    )
    if model in ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"):
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                           fallbacks="default", **kwargs)
    else:
        resp = client.messages.create(**kwargs)
    call = {"file_id": ev.file_id, "requested_model": model, "served_model": resp.model,
            "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
            "stop_reason": resp.stop_reason,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    usage_calls.append(call)
    if resp.stop_reason != "end_turn":
        print(f"multimodal: WARNING {ev.file_id}: stop_reason={resp.stop_reason}; not cached", file=log)
        return None
    text = next(b.text for b in resp.content if b.type == "text")
    records = json.loads(text)["records"]
    entry = {"file_id": ev.file_id, "file_path": ev.rel_path, "file_sha256": _sha256(ev.path),
             "extractor": {"method": "api", "model": model, "served_model": resp.model,
                           "prompt_version": PROMPT_VERSION},
             "usage": {"input_tokens": call["input_tokens"], "output_tokens": call["output_tokens"]},
             "records": records}
    path = cache_dir / f"{entry['file_sha256'][:16]}__api_{model}_{PROMPT_VERSION}.json"
    path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return entry


# ------------------------------------------------------------------ validation
def records_to_observations(ev: EvidenceFile, records: list[dict],
                            aliases: dict[str, tuple[str, str]], log=sys.stderr) -> list[Observation]:
    """Validate transcribed records and convert them. Records whose state or
    category is outside the allowed vocabulary, or whose resource cannot be
    named explicitly, are dropped with a warning."""
    out = []
    for i, r in enumerate(records):
        rid, system = _resolve(r.get("keys") or [], aliases)
        state = str(r.get("state", "")).upper()
        cat = str(r.get("category", "")).upper() or None
        problem = None
        if rid is None:
            problem = f"no resource id or catalog alias in keys {r.get('keys')}"
        elif state not in RESULT_STATES:
            problem = f"invalid state {state!r}"
        elif cat is not None and cat not in CATEGORIES:
            problem = f"invalid category {cat!r}"
        if problem:
            print(f"multimodal: {ev.file_id} record {i} dropped: {problem}", file=log)
            continue
        ids = {n for n in (normalize_identifier(x) for x in r.get("identifiers") or []) if n}
        observed = ev.captured_at
        t = r.get("observed_at") or ""
        try:
            if t and parse_ts(t) <= parse_ts(ev.captured_at):
                observed = t
        except ValueError:
            pass
        system = system or r.get("system_id") or SYSTEM_FOR_CATEGORY.get(cat or "", "")
        out.append(Observation(
            resource_id=rid, system_id=system, category=cat, state=state, identifiers=ids,
            observed_at=observed, evidence_id=ev.file_id, source_kind="image",
            raw={"keys": r.get("keys"), "notes": r.get("notes", ""), "extractor": "vision"},
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


def _write_usage(cache_dir: Path, calls: list, used: list, skipped: list) -> None:
    report = {
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "api_calls_this_run": calls,
        "api_calls_count": len(calls),
        "input_tokens_this_run": sum(c["input_tokens"] for c in calls),
        "output_tokens_this_run": sum(c["output_tokens"] for c in calls),
        "files_used": used,
        "files_skipped": skipped,
    }
    (cache_dir / "usage.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")
