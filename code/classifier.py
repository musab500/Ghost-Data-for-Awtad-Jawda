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
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROMPT_VERSION = "v1"
DEFAULT_MODEL = os.environ.get("GHOST_CLASSIFIER_MODEL", "claude-haiku-5-5")
_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
_CACHE_PATH = _CACHE_DIR / "classifier.json"            # raw LLM verdicts, keyed by hash
USAGE_PATH = _CACHE_DIR / "classifier_usage.json"       # one record per real API call
_REPO_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> bool:
    """Load KEY=VALUE lines from a gitignored .env into os.environ (existing
    variables win). Values are never printed or logged. Returns True if loaded."""
    path = path or (_REPO_ROOT / ".env")
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return True

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
        load_dotenv()
        self.model = model
        self.llm_errors: list[str] = []
        self.cache_hits = 0
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
    def _record_usage(self, rec: dict) -> None:
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            data = json.loads(USAGE_PATH.read_text(encoding="utf-8")) if USAGE_PATH.exists() else []
            data.append(rec)
            USAGE_PATH.write_text(json.dumps(data, indent=0), encoding="utf-8")
        except Exception as e:  # usage is a deliverable: do not lose it silently
            print(f"WARNING: could not record classifier usage: {e}", file=sys.stderr)

    def llm_verdict(self, text: str) -> ClassifierResult | None:
        """Raw LLM verdict (no override), cached by sha256(text+model+prompt).
        Returns None when no key is configured or the call fails (error recorded
        in ``self.llm_errors`` and printed to stderr; the key is never included)."""
        key = self._key(text)
        c = self._cache.get(key)
        if c is not None:
            self.cache_hits += 1
            return ClassifierResult(c["is_deletion"], c["confidence"], c["reason"], "llm")
        if not self.use_llm:
            return None
        try:
            if self._client is None:
                import anthropic  # lazy; only when a key is present
                self._client = anthropic.Anthropic(api_key=self._api_key)
            msg = self._client.messages.create(
                model=self.model, max_tokens=200, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}])
            u = getattr(msg, "usage", None)
            rec = {"model": getattr(msg, "model", None) or self.model,
                   "input_tokens": getattr(u, "input_tokens", 0) or 0,
                   "output_tokens": getattr(u, "output_tokens", 0) or 0,
                   "text_sha256": hashlib.sha256(_norm(text).encode()).hexdigest(),
                   "prompt_version": PROMPT_VERSION, "ts": int(time.time())}
            self.calls += 1
            self.input_tokens += rec["input_tokens"]
            self.output_tokens += rec["output_tokens"]
            self._record_usage(rec)
            raw = "".join(getattr(b, "text", "") for b in msg.content)
            data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
            res = ClassifierResult(bool(data["is_deletion"]), float(data.get("confidence", 0.5)),
                                   str(data.get("reason", "")), "llm")
        except Exception as e:
            msg_ = f"{type(e).__name__}: {str(e)[:200]}"
            self.llm_errors.append(msg_)
            print(f"WARNING: LLM classification failed, using fallback ({msg_})", file=sys.stderr)
            return None
        self._cache[key] = {"is_deletion": res.is_deletion, "confidence": res.confidence,
                            "reason": res.reason}
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
