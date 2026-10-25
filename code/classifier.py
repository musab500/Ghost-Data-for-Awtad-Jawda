"""Request classifier (Person B).

Decides whether a request's free-text actually authorizes erasure. A request
for access, a data copy, a correction, a marketing opt-out, or login help is
NOT a deletion request and must be answered NOT_APPLICABLE with an empty step
list — even when its other fields resemble a deletion case.

Two layers:

1. An LLM classifier (optional) through ``llm_provider.complete_json`` (Gemini or Anthropic,
   chosen by environment; see llm_provider.py). It is used only when a provider key is
   configured. Verdicts are cached by ``sha256(text + provider + model + prompt version)`` so a
   given request text is classified at most once and full runs are cheap and reproducible. A
   failed, blocked, empty or malformed response is NEVER cached as a success: the text falls
   back to the deterministic rules for that run and is retried on the next one. Real per-call
   usage (from the API response only) is appended to code/.cache/classifier_usage.json.

2. A deterministic negation / intent fallback. It runs when no API key is
   available, and it also acts as a safety override on top of the LLM: an
   explicit non-deletion statement ("I am not asking for deletion", "do not
   close the account", "do not delete ...") forces a NOT_APPLICABLE result even
   if the model disagreed.

``flagged_cases`` (from the CLASSIFY_REQUEST workflow rows) is a *claim* and is
used only to cross-check this classifier in the evaluation report, never as the
decision.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import llm_provider
from llm_provider import load_dotenv, LLMFailure  # noqa: F401  (load_dotenv re-exported)

PROMPT_VERSION = "v2"
_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
_CACHE_PATH = _CACHE_DIR / "classifier.json"            # raw LLM verdicts, keyed by hash
USAGE_PATH = _CACHE_DIR / "classifier_usage.json"       # one record per API response
HARD_FAILURE_KINDS = {"auth", "not_found", "rate_limit", "no_key"}
HARD_FAILURE_LIMIT = 3          # this many in a row switch the LLM off for the rest of the run

SYSTEM_PROMPT = (
    "You classify customer privacy requests, which may be written in any language. Decide whether "
    "the message authorizes DELETION or ERASURE of the person's personal data or account. "
    "Requests for data access, a copy or export of data, a correction, a marketing or email "
    "opt-out, login help, or any request that explicitly says not to delete or to keep the "
    "account are NOT deletion requests. Return is_deletion, a confidence between 0 and 1, and a "
    "short reason in English."
)
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"is_deletion": {"type": "boolean"},
                   "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                   "reason": {"type": "string"}},
    "required": ["is_deletion", "confidence", "reason"],
}

# -- deterministic fallback signals ---------------------------------------- #
# Explicit statements that the person does NOT want deletion. Any of these is a
# hard override to NOT_APPLICABLE regardless of the LLM.
_HARD_NEGATION = [
    "not asking for deletion",
    "not a request to erase",
    "not a request to delete",
    "don't delete or change anything",
    "do not delete or change anything",
    "do not close the account",
    "don't close the account",
    "do not delete",
    "don't delete",
    "without removing any",
    "without deleting",
    "not to erase",
    "rather than deleting",
]

# Intent phrases for the non-deletion request types in scope (access, copy,
# correction, marketing opt-out, login help, keep-account).
_NON_DELETION_INTENT = [
    # keep / retain the account
    "leave the account active",
    "leave my profile",
    "keep the account",
    "keep my account",
    "continue using the service",
    "still use it",
    "still need the account",
    "still want receipts",
    "in place",
    # data access / copy / export
    "download my account information",
    "portable format",
    "copy of the information",
    "copy of last year",
    "copy of my invoices",
    "copy of my data",
    "copy of my records",
    "what personal information you hold",
    "information request",
    "send me a copy",
    # correction
    "is misspelled",
    "surname is misspelled",
    "correct it without",
    "out of date. please replace",
    "replace it with the new one",
    "change the email address",
    # marketing opt-out
    "marketing lists only",
    "stop the promotional emails",
    "remove me from marketing",
    # login help
    "resetting the login",
    "reset the login",
    "reset my login",
]

# Positive deletion cues (used to resolve ambiguity in the fallback only).
_DELETION_CUES = [
    "delete my account", "delete my data", "delete all", "erase my",
    "erase all", "remove my account", "close my account and delete",
    "right to be forgotten", "forget me", "wipe my", "permanently remove",
    "gdpr erasure", "erasure request", "delete everything",
]


_FALLBACK_NOTICE_SHOWN = False


@dataclass
class ClassifierResult:
    is_deletion: bool
    confidence: float
    reason: str
    source: str  # "llm", "fallback", or "llm+override"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _hard_negation(text: str) -> str | None:
    low = _norm(text)
    for p in _HARD_NEGATION:
        if p in low:
            return p
    return None


def fallback_classify(text: str) -> ClassifierResult:
    """Deterministic intent classifier. Conservative: defaults to deletion
    unless a clear non-deletion intent is present, so real erasure requests are
    never silently dropped."""
    low = _norm(text)
    neg = _hard_negation(low)
    if neg:
        return ClassifierResult(False, 0.97, f"explicit non-deletion: '{neg}'", "fallback")
    hit = next((p for p in _NON_DELETION_INTENT if p in low), None)
    if hit:
        # A non-deletion intent phrase, and no overriding deletion cue.
        if not any(c in low for c in _DELETION_CUES):
            return ClassifierResult(False, 0.9, f"non-deletion intent: '{hit}'", "fallback")
    return ClassifierResult(True, 0.75, "no non-deletion signal", "fallback")


class RequestClassifier:
    def __init__(self, use_llm: bool | None = None):
        llm_provider.load_dotenv()      # via the module, so tests can patch it
        self.llm_errors: list[str] = []
        self.cache_hits = 0
        self.calls = 0                    # API responses received this run (incl. unusable ones)
        self.input_tokens = 0
        self.output_tokens = 0
        self.llm_disabled_reason = ""
        self._hard_failures = 0
        self._failed_keys: set[str] = set()   # texts that failed this run: skipped, retried next run
        if use_llm is None:
            use_llm = llm_provider.is_configured()
        self.use_llm = bool(use_llm) and llm_provider.is_configured()
        self.provider = llm_provider.provider_name() if self.use_llm else "none"
        self.model = llm_provider.model_for("classifier") if self.use_llm else ""
        self._cache = self._load_cache()
        global _FALLBACK_NOTICE_SHOWN
        if not self.use_llm and not _FALLBACK_NOTICE_SHOWN:
            _FALLBACK_NOTICE_SHOWN = True
            print("NOTICE: request classifier is running in FALLBACK mode "
                  "(deterministic phrase rules, 0 model calls): no LLM provider key is configured "
                  "(set GEMINI_API_KEY or ANTHROPIC_API_KEY).", file=sys.stderr)

    # -- cache -------------------------------------------------------------
    def _load_cache(self) -> dict:
        try:
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cache(self) -> None:
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(json.dumps(self._cache, indent=0), encoding="utf-8")
        except Exception as e:
            print(f"WARNING: could not save classifier cache: {e}", file=sys.stderr)

    def _key(self, text: str) -> str:
        h = hashlib.sha256()
        for part in (_norm(text), self.provider, self.model, PROMPT_VERSION):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    # -- usage ---------------------------------------------------------------
    def _record_usage(self, usage: dict, text: str, ok: bool, kind: str = "") -> None:
        """Persist what the API reported, one record per API response (a truncation retry is its
        own record). Failed-but-answered responses are recorded too: their tokens were billed.
        Nothing is estimated."""
        attempts = usage.get("attempts") or [{**usage, "ok": ok, "failure": kind, "retry": False}]
        sha = hashlib.sha256(_norm(text).encode()).hexdigest()
        recs = []
        for a in attempts:
            self.calls += 1
            self.input_tokens += a.get("input_tokens") or 0
            self.output_tokens += a.get("output_tokens") or 0
            rec = {k: a.get(k) for k in ("provider", "model", "requested_model", "input_tokens", "output_tokens",
                                         "usage_reported", "max_output_tokens")}
            rec.update({"ok": a.get("ok"), "failure": a.get("failure") or "", "retry": bool(a.get("retry")),
                        "prompt_version": PROMPT_VERSION, "ts": int(time.time()), "text_sha256": sha})
            recs.append(rec)
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            data = json.loads(USAGE_PATH.read_text(encoding="utf-8")) if USAGE_PATH.exists() else []
            data.extend(recs)
            USAGE_PATH.write_text(json.dumps(data, indent=0), encoding="utf-8")
        except Exception as e:  # usage is a deliverable: never lose it silently
            print(f"WARNING: could not record classifier usage: {e}", file=sys.stderr)

    # -- llm ---------------------------------------------------------------
    def llm_verdict(self, text: str) -> ClassifierResult | None:
        """Raw LLM verdict (no override). Served from the cache when present; otherwise one call
        through llm_provider. Returns None when no provider is configured or the call produced
        nothing usable (the reason is in ``llm_errors`` and on stderr); None is never cached."""
        if not self.use_llm:
            return None
        key = self._key(text)
        c = self._cache.get(key)
        if c is not None:
            self.cache_hits += 1
            return ClassifierResult(c["is_deletion"], c["confidence"], c["reason"], "llm")
        if self.llm_disabled_reason or key in self._failed_keys:
            return None
        try:
            parsed, usage = llm_provider.complete_json(SYSTEM_PROMPT, text, RESPONSE_SCHEMA)
        except LLMFailure as f:
            self._failed_keys.add(key)
            if f.usage is not None:
                self._record_usage(f.usage, text, False, f.kind)
            self.llm_errors.append(f"{f.kind}: {f.message[:160]}")
            print(f"WARNING: LLM classification skipped, using fallback for this text ({f.kind}: {f.message[:160]})",
                  file=sys.stderr)
            if f.kind in HARD_FAILURE_KINDS:
                self._hard_failures += 1
                if self._hard_failures >= HARD_FAILURE_LIMIT:
                    self.llm_disabled_reason = f"{HARD_FAILURE_LIMIT} consecutive '{f.kind}' failures"
                    print(f"WARNING: LLM switched off for the rest of this run ({self.llm_disabled_reason}); "
                          f"remaining texts use the fallback. Fix the cause and re-run: cached verdicts are kept.",
                          file=sys.stderr)
            else:
                self._hard_failures = 0
            return None
        except Exception as e:  # a bug must not crash the whole run
            self._failed_keys.add(key)
            self.llm_errors.append(f"unexpected {type(e).__name__}")
            print(f"WARNING: unexpected {type(e).__name__} from the LLM layer; using fallback", file=sys.stderr)
            return None
        self._hard_failures = 0
        self._record_usage(usage, text, True)
        res = ClassifierResult(bool(parsed["is_deletion"]), float(parsed["confidence"]),
                               str(parsed["reason"]), "llm")
        self._cache[key] = {"is_deletion": res.is_deletion, "confidence": res.confidence,
                            "reason": res.reason, "provider": usage.get("provider"),
                            "model": usage.get("model")}
        self._save_cache()
        return res

    # -- public ------------------------------------------------------------
    def classify(self, text: str) -> ClassifierResult:
        """Combined decision: LLM verdict when available, with the explicit
        negation override on top; deterministic fallback otherwise."""
        result = self.llm_verdict(text)
        if result is not None:
            neg = _hard_negation(text)
            if neg and result.is_deletion:
                return ClassifierResult(False, max(result.confidence, 0.95),
                                        f"override, explicit non-deletion: '{neg}'", "llm+override")
            return result
        return fallback_classify(text)

    def is_deletion(self, text: str) -> bool:
        return self.classify(text).is_deletion


if __name__ == "__main__":
    # Quick smoke test on the hard cases.
    clf = RequestClassifier(use_llm=False)
    for t in [
        "Please delete my account and erase all my data.",
        "Please stop the promotional emails. I still need the account, so I am not asking for deletion.",
        "I'd like to download my account information in a portable format. Please keep the account as it is.",
        "My surname is misspelled on the account. Can you correct it without removing any of my records?",
    ]:
        r = clf.classify(t)
        print(f"{r.is_deletion!s:5} {r.confidence:.2f} {r.source:12} {r.reason}  | {t[:60]}")
