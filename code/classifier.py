"""Request classifier (Person B).

Decides whether a request's free-text actually authorizes erasure. A request
for access, a data copy, a correction, a marketing opt-out, or login help is
NOT a deletion request and must be answered NOT_APPLICABLE with an empty step
list — even when its other fields resemble a deletion case.

Two layers:

1. An LLM classifier (optional). It is used only when an API key is present in
   the environment. Results are cached by ``sha256(text + model + prompt
   version)`` so a given request text is classified at most once, and the cache
   makes full-dataset runs cheap and reproducible. The API key is read from the
   environment only and never logged or cached.

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
from dataclasses import dataclass
from pathlib import Path

PROMPT_VERSION = "v1"
DEFAULT_MODEL = os.environ.get("GHOST_CLASSIFIER_MODEL", "claude-haiku-5-5")
_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
_CACHE_PATH = _CACHE_DIR / "classifier.json"

SYSTEM_PROMPT = (
    "You classify customer privacy requests. Decide whether the message is a "
    "request to DELETE or ERASE the person's personal data / account. "
    "Requests for data access, a copy/export, a correction, a marketing or "
    "email opt-out, login help, or any request that explicitly says not to "
    "delete or to keep the account are NOT deletion requests. "
    'Reply with strict JSON: {"is_deletion": true|false, "confidence": 0.0-1.0, '
    '"reason": "<short reason>"}.'
)

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
    def __init__(self, model: str = DEFAULT_MODEL, use_llm: bool | None = None):
        self.model = model
        self._cache = self._load_cache()
        self._api_key = (os.environ.get("ANTHROPIC_API_KEY")
                         or os.environ.get("CLAUDE_API_KEY") or "")
        if use_llm is None:
            use_llm = bool(self._api_key)
        self.use_llm = use_llm
        self._client = None
        # usage accounting for the usage report
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

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
        except Exception:
            pass

    def _key(self, text: str) -> str:
        h = hashlib.sha256()
        h.update(_norm(text).encode("utf-8"))
        h.update(b"\x00")
        h.update(self.model.encode("utf-8"))
        h.update(b"\x00")
        h.update(PROMPT_VERSION.encode("utf-8"))
        return h.hexdigest()

    # -- llm ---------------------------------------------------------------
    def _llm_classify(self, text: str) -> ClassifierResult | None:
        try:
            if self._client is None:
                import anthropic  # lazy; only when a key is present
                self._client = anthropic.Anthropic(api_key=self._api_key)
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=200,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
            self.calls += 1
            u = getattr(msg, "usage", None)
            if u is not None:
                self.input_tokens += getattr(u, "input_tokens", 0) or 0
                self.output_tokens += getattr(u, "output_tokens", 0) or 0
            raw = "".join(getattr(b, "text", "") for b in msg.content)
            data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            return ClassifierResult(bool(data["is_deletion"]),
                                    float(data.get("confidence", 0.5)),
                                    str(data.get("reason", "")), "llm")
        except Exception:
            return None

    # -- public ------------------------------------------------------------
    def classify(self, text: str) -> ClassifierResult:
        key = self._key(text)
        if key in self._cache:
            c = self._cache[key]
            return ClassifierResult(c["is_deletion"], c["confidence"], c["reason"], c["source"])

        result = None
        if self.use_llm:
            result = self._llm_classify(text)
            if result is not None:
                # Safety override: an explicit non-deletion statement wins.
                neg = _hard_negation(text)
                if neg and result.is_deletion:
                    result = ClassifierResult(False, max(result.confidence, 0.95),
                                              f"override, explicit non-deletion: '{neg}'",
                                              "llm+override")
        if result is None:
            result = fallback_classify(text)

        self._cache[key] = {
            "is_deletion": result.is_deletion, "confidence": result.confidence,
            "reason": result.reason, "source": result.source,
        }
        self._save_cache()
        return result

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
