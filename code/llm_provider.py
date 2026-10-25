"""One small LLM provider layer (Person B): Gemini or Anthropic behind one interface.

    complete_json(system, user, schema=None)                       -> (parsed, usage)
    vision_json(system, prompt, image_bytes, mime, schema)         -> (parsed, usage)

    usage = {"provider", "model", "input_tokens", "output_tokens"}  # from the API response ONLY
            (+ "requested_model", "usage_reported")

Provider selection (environment or the gitignored .env; keys are never printed or logged):
    LLM_PROVIDER      gemini | anthropic | none
                      default: gemini if GEMINI_API_KEY is set, else anthropic if ANTHROPIC_API_KEY
                      (or CLAUDE_API_KEY) is set, else none
    GEMINI_API_KEY    Gemini Developer API key
    CLASSIFIER_MODEL  model for complete_json callers (provider default below)
    VISION_MODEL      model for vision_json callers   (provider default below)

Failure model. Any failure raises ``LLMFailure`` (never returns a half-result):
    kind in no_key | auth | not_found | rate_limit | transient | blocked | truncated | empty |
            malformed | schema | no_usage | bad_request | other
It carries ``.usage`` when the API answered (tokens may have been spent even though the answer
is unusable), so callers can still account for them. Callers skip the item and must NOT cache a
failure as success. Transient errors (HTTP 408/429/5xx, timeouts, connection errors) are retried
with a small backoff and a hard cap (MAX_ATTEMPTS); auth / not-found / bad-request errors are not.

Truncation. If a response is cut off (Gemini MAX_TOKENS / Anthropic max_tokens) the call is retried ONCE
with RETRY_MAX_OUTPUT_TOKENS; a truncated answer is never parsed, returned or cached. If the retry is
also cut off the call fails with kind "truncated". The returned (or failure) ``usage`` sums the tokens of
every response and carries ``usage["attempts"]``: one entry per API response with ok / failure /
max_output_tokens / retry, so callers can write each retry to their usage file.

Token accounting. Gemini bills thinking tokens as output, and reports them separately:
output_tokens = candidates_token_count + thoughts_token_count. input_tokens = prompt_token_count.
If a response carries no usage metadata the call is reported with usage_reported=False and zero
tokens; nothing is estimated.

Model names. Defaults come from the official google-genai SDK's enumerated model list. They have
NOT been confirmed by a live call in the environment this was written in (no key was available);
run ``python3 tools/check_env.py --ping`` with a key to confirm, or set CLASSIFIER_MODEL /
VISION_MODEL.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

MAX_ATTEMPTS = 3                 # hard cap per call (1 try + 2 retries)
BACKOFF_SECONDS = (1.0, 3.0)     # sleep before retry 1 and retry 2
TIMEOUT_SECONDS = 90
_sleep = time.sleep              # replaced in tests

DEFAULT_MODELS = {
    "gemini": {"classifier": "gemini-3.5-flash-lite", "vision": "gemini-3.6-flash"},
    "anthropic": {"classifier": "claude-haiku-5-5", "vision": "claude-haiku-5-5"},
}
TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}
MAX_OUTPUT_TOKENS = {"classifier": 2048, "vision": 8192}
# One retry with a higher limit when the answer was cut off (finish reason MAX_TOKENS / max_tokens).
RETRY_MAX_OUTPUT_TOKENS = {"classifier": 4096, "vision": 16384}


# --------------------------------------------------------------------------- #
#  .env loading
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path | None = None) -> bool:
    """Load KEY=VALUE lines from a gitignored .env into os.environ (existing variables win).
    Values are never printed or logged. Returns True if a file was read."""
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


# --------------------------------------------------------------------------- #
#  Failure type
# --------------------------------------------------------------------------- #
class LLMFailure(Exception):
    """A call produced no usable result. ``usage`` is set when the API did respond."""

    def __init__(self, kind: str, message: str, usage: dict | None = None, attempts: int = 1):
        super().__init__(f"[{kind}] {message}")
        self.kind, self.message, self.usage, self.attempts = kind, message, usage, attempts


def _scrub(text: str) -> str:
    """Remove anything that looks like one of our keys from an error message."""
    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        val = os.environ.get(var)
        if val and len(val) >= 8:
            text = text.replace(val, "[REDACTED]")
    return re.sub(r"(AIza[0-9A-Za-z_\-]{20,}|AQ\.[0-9A-Za-z_\-]{20,}|sk-ant-[0-9A-Za-z_\-]{10,})", "[REDACTED]", text)


# --------------------------------------------------------------------------- #
#  Response validation (JSON parse + a small JSON-Schema subset)
# --------------------------------------------------------------------------- #
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(value, t: str) -> bool:
    t = t.lower()
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t]) if t in _TYPES else True


def validate_schema(value, schema: dict, path: str = "$") -> list[str]:
    """Errors for ``value`` against a JSON-Schema subset: type, enum, required, properties,
    additionalProperties=false, items, minimum, maximum, anyOf."""
    errs: list[str] = []
    if "anyOf" in schema:
        if not any(not validate_schema(value, s, path) for s in schema["anyOf"]):
            errs.append(f"{path}: matches none of anyOf")
        return errs
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, dict):
        for r in schema.get("required", []):
            if r not in value:
                errs.append(f"{path}: missing required '{r}'")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                errs += validate_schema(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected property '{k}'")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += validate_schema(v, schema["items"], f"{path}[{i}]")
    return errs


def parse_json_text(text: str | None, schema: dict | None, usage: dict) -> dict:
    """JSON-parse model text (tolerating a ```json fence) and check it against ``schema``."""
    if text is None or not text.strip():
        raise LLMFailure("empty", "model returned no text", usage)
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, flags=re.S)
    if m:
        t = m.group(1)
    try:
        parsed = json.loads(t)
    except json.JSONDecodeError as e:
        raise LLMFailure("malformed", f"response is not valid JSON ({e.msg} at char {e.pos})", usage)
    if not isinstance(parsed, dict):
        raise LLMFailure("malformed", f"expected a JSON object, got {type(parsed).__name__}", usage)
    if schema:
        errs = validate_schema(parsed, schema)
        if errs:
            raise LLMFailure("schema", "; ".join(errs[:3]), usage)
    return parsed


# --------------------------------------------------------------------------- #
#  Error classification and retries (duck-typed so no SDK import is needed)
# --------------------------------------------------------------------------- #
def _status_code(exc: Exception) -> int | None:
    for attr in ("code", "status_code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    return None


def classify_exception(exc: Exception) -> tuple[str, bool]:
    """-> (kind, retryable)"""
    code, name, msg = _status_code(exc), type(exc).__name__, str(exc)
    if code == 429:
        return "rate_limit", True
    if code in TRANSIENT_CODES:
        return "transient", True
    if re.search(r"Timeout|Connection|Transport|Network|Disconnected", name):
        return "transient", True
    if code in (401, 403) or re.search(r"API key not valid|API_KEY_INVALID|invalid x-api-key|authentication", msg, re.I):
        return "auth", False
    if code == 404:
        return "not_found", False
    if code == 400:
        return "bad_request", False
    return "other", False


def _with_retries(call, model: str):
    """Run ``call()``; retry transient errors with backoff up to MAX_ATTEMPTS. LLMFailure from
    the call itself (blocked / malformed / ...) is never retried: it is deterministic content."""
    last_kind, last_msg = "other", ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return call()
        except LLMFailure as f:
            f.attempts = attempt
            raise
        except Exception as e:  # SDK / network errors
            kind, retryable = classify_exception(e)
            last_kind, last_msg = kind, _scrub(f"{type(e).__name__}: {str(e)[:300]}")
            if not retryable:
                hint = {"auth": " (check the API key)",
                        "not_found": f" (model {model!r} not found: set CLASSIFIER_MODEL / VISION_MODEL)"}.get(kind, "")
                raise LLMFailure(kind, last_msg + hint, None, attempt)
            if attempt < MAX_ATTEMPTS:
                _sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
    if last_kind == "rate_limit":
        raise LLMFailure("rate_limit", f"rate limited (HTTP 429) after {MAX_ATTEMPTS} attempts; wait, lower "
                         f"the request rate or raise the quota. Last error: {last_msg}", None, MAX_ATTEMPTS)
    raise LLMFailure("transient", f"still failing after {MAX_ATTEMPTS} attempts: {last_msg}", None, MAX_ATTEMPTS)


# --------------------------------------------------------------------------- #
#  Truncation retry: ONE retry with a higher output limit, never a partial answer
# --------------------------------------------------------------------------- #
def _attempt_entry(usage: dict, ok: bool, kind: str, limit: int, retry: bool) -> dict:
    e = {k: usage.get(k) for k in ("provider", "model", "requested_model", "input_tokens",
                                   "output_tokens", "usage_reported")}
    e.update({"ok": ok, "failure": kind, "max_output_tokens": limit, "retry": retry})
    return e


def _merge_attempts(attempts: list[dict]) -> dict:
    """Usage for the whole call: tokens summed over every API response (all were billed), plus the
    per-response ``attempts`` list so callers can record each retry separately."""
    last = attempts[-1]
    return {"provider": last["provider"], "model": last["model"], "requested_model": last["requested_model"],
            "input_tokens": sum(a["input_tokens"] or 0 for a in attempts),
            "output_tokens": sum(a["output_tokens"] or 0 for a in attempts),
            "usage_reported": all(a["usage_reported"] for a in attempts), "attempts": attempts}


def _with_truncation_retry(role: str, run) -> tuple[dict, dict]:
    """``run(limit) -> (parsed, usage)``. If the first response was truncated, call once more with
    RETRY_MAX_OUTPUT_TOKENS[role]. A truncated response is never parsed or returned; if the retry is
    also truncated the call fails with kind 'truncated'. Every response's tokens are kept."""
    attempts: list[dict] = []
    for i, limit in enumerate((MAX_OUTPUT_TOKENS[role], RETRY_MAX_OUTPUT_TOKENS[role])):
        try:
            parsed, usage = run(limit)
        except LLMFailure as f:
            if f.usage:
                attempts.append(_attempt_entry(f.usage, False, f.kind, limit, i > 0))
            if f.kind == "truncated" and i == 0:
                continue
            f.usage = _merge_attempts(attempts) if attempts else None
            raise
        attempts.append(_attempt_entry(usage, True, "", limit, i > 0))
        return parsed, _merge_attempts(attempts)
    raise AssertionError("unreachable")  # pragma: no cover

# --------------------------------------------------------------------------- #
#  Providers
# --------------------------------------------------------------------------- #
# Models that reject a thinking budget (HTTP 400 INVALID_ARGUMENT): the default list comes from a
# diagnostic call (gemini-3.5-flash-lite answers without the setting and fails with it); further
# models are learned in-process on the first 400 and then called without the setting.
DEFAULT_NO_THINKING_MODELS = "gemini-3.5-flash-lite"
_NO_THINKING_LEARNED: set[str] = set()


def _no_thinking_models() -> set[str]:
    listed = os.environ.get("VISION_NO_THINKING_MODELS", DEFAULT_NO_THINKING_MODELS)
    return {m.strip() for m in listed.split(",") if m.strip()} | _NO_THINKING_LEARNED


def _vision_thinking_budget() -> int | None:
    """Thinking-token budget for vision calls (VISION_THINKING_BUDGET): 0 = off (default); an
    integer sets the budget; "default" leaves the model's own setting."""
    v = os.environ.get("VISION_THINKING_BUDGET", "0").strip().lower()
    if v in ("default", "none", ""):
        return None
    try:
        return max(0, int(v))
    except ValueError:
        return 0


def _usage(provider: str, model: str, requested: str, tin, tout, reported: bool) -> dict:
    return {"provider": provider, "model": model or requested, "requested_model": requested,
            "input_tokens": int(tin or 0), "output_tokens": int(tout or 0), "usage_reported": reported}


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, client=None):
        self._api_key, self._client = api_key, client

    def model_for(self, role: str) -> str:
        return os.environ.get("VISION_MODEL" if role == "vision" else "CLASSIFIER_MODEL") \
            or DEFAULT_MODELS["gemini"][role]

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types
            self._client = genai.Client(api_key=self._api_key,
                                        http_options=types.HttpOptions(timeout=TIMEOUT_SECONDS * 1000))
        return self._client

    def _generate(self, role: str, system: str, contents, schema: dict | None) -> tuple[dict, dict]:
        return _with_truncation_retry(role, lambda limit: self._once(role, system, contents, schema, limit))

    def _once(self, role: str, system: str, contents, schema: dict | None, limit: int) -> tuple[dict, dict]:
        from google.genai import types
        model = self.model_for(role)
        cfg = {"system_instruction": system, "response_mime_type": "application/json",
               "max_output_tokens": limit, "temperature": 0}
        if schema:
            cfg["response_json_schema"] = schema
        budget = _vision_thinking_budget() if role == "vision" and model not in _no_thinking_models() else None
        if budget is not None:     # transcription needs no reasoning: spend (almost) no thinking tokens
            cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)

        def call():
            try:
                resp = self._get_client().models.generate_content(
                    model=model, contents=contents, config=types.GenerateContentConfig(**cfg))
            except Exception as e:  # noqa: BLE001 - a model that rejects the thinking setting runs without it
                if "thinking_config" not in cfg or classify_exception(e)[0] != "bad_request":
                    raise
                cfg.pop("thinking_config")
                _NO_THINKING_LEARNED.add(model)
                resp = self._get_client().models.generate_content(
                    model=model, contents=contents, config=types.GenerateContentConfig(**cfg))
            return self._interpret(resp, model, schema)

        return _with_retries(call, model)

    def _interpret(self, resp, requested: str, schema: dict | None) -> tuple[dict, dict]:
        um = getattr(resp, "usage_metadata", None)
        if um is not None:
            usage = _usage("gemini", getattr(resp, "model_version", None), requested,
                           um.prompt_token_count,
                           (um.candidates_token_count or 0) + (um.thoughts_token_count or 0), True)
        else:
            usage = _usage("gemini", getattr(resp, "model_version", None), requested, 0, 0, False)
        fb = getattr(resp, "prompt_feedback", None)
        if fb is not None and getattr(fb, "block_reason", None):
            raise LLMFailure("blocked", f"prompt blocked: {_enum_name(fb.block_reason)}"
                             f"{(' - ' + fb.block_reason_message) if getattr(fb, 'block_reason_message', None) else ''}", usage)
        cands = getattr(resp, "candidates", None) or []
        if not cands:
            raise LLMFailure("empty", "response has no candidates", usage)
        cand = cands[0]
        fr = _enum_name(getattr(cand, "finish_reason", None))
        if fr == "MAX_TOKENS":
            raise LLMFailure("truncated", "output hit max_output_tokens before finishing", usage)
        if fr not in ("", "STOP", "FINISH_REASON_UNSPECIFIED"):
            raise LLMFailure("blocked", f"generation stopped: {fr}", usage)
        parts = getattr(getattr(cand, "content", None), "parts", None) or []
        text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False))
        parsed = parse_json_text(text, schema, usage)
        if not usage["usage_reported"]:
            usage["note"] = "response carried no usage metadata; tokens not counted"
        return parsed, usage

    def complete_json(self, system, user, schema=None):
        return self._generate("classifier", system, user, schema)

    def vision_json(self, system, prompt, image_bytes, mime, schema):
        from google.genai import types
        contents = [types.Part.from_bytes(data=image_bytes, mime_type=mime), prompt]
        return self._generate("vision", system, contents, schema)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, client=None):
        self._api_key, self._client = api_key, client

    def model_for(self, role: str) -> str:
        return os.environ.get("VISION_MODEL" if role == "vision" else "CLASSIFIER_MODEL") \
            or (os.environ.get("GHOST_CLASSIFIER_MODEL") if role == "classifier" else None) \
            or DEFAULT_MODELS["anthropic"][role]

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key, timeout=float(TIMEOUT_SECONDS))
        return self._client

    def _generate(self, role: str, system: str, content, schema: dict | None) -> tuple[dict, dict]:
        return _with_truncation_retry(role, lambda limit: self._once(role, system, content, schema, limit))

    def _once(self, role: str, system: str, content, schema: dict | None, limit: int) -> tuple[dict, dict]:
        model = self.model_for(role)
        sys_prompt = system + "\n\nRespond with ONLY a single JSON object, no prose and no code fences."
        if schema:
            sys_prompt += "\nIt must satisfy this JSON Schema:\n" + json.dumps(schema)

        def call():
            msg = self._get_client().messages.create(
                model=model, max_tokens=limit, system=sys_prompt,
                messages=[{"role": "user", "content": content}])
            u = getattr(msg, "usage", None)
            usage = _usage("anthropic", getattr(msg, "model", None), model,
                           getattr(u, "input_tokens", 0), getattr(u, "output_tokens", 0), u is not None)
            stop = getattr(msg, "stop_reason", None)
            if stop == "refusal":
                raise LLMFailure("blocked", "model refused the request", usage)
            if stop == "max_tokens":
                raise LLMFailure("truncated", "output hit max_tokens before finishing", usage)
            text = "".join(getattr(b, "text", "") or "" for b in (getattr(msg, "content", None) or []))
            return parse_json_text(text, schema, usage), usage

        return _with_retries(call, model)

    def complete_json(self, system, user, schema=None):
        return self._generate("classifier", system, user, schema)

    def vision_json(self, system, prompt, image_bytes, mime, schema):
        content = [{"type": "image", "source": {"type": "base64", "media_type": mime,
                                                 "data": base64.b64encode(image_bytes).decode("ascii")}},
                   {"type": "text", "text": prompt}]
        return self._generate("vision", system, content, schema)


def _enum_name(v) -> str:
    return "" if v is None else str(getattr(v, "name", v)).split(".")[-1]


# --------------------------------------------------------------------------- #
#  Selection and the public functions
# --------------------------------------------------------------------------- #
_override = None
_instances: dict = {}


def set_provider(provider) -> None:
    """Install a provider object (tests use a fake; it needs name, model_for, complete_json, vision_json)."""
    global _override
    _override = provider


def reset_provider() -> None:
    global _override
    _override = None
    _instances.clear()


def _key(provider: str) -> str:
    if provider == "gemini":
        return os.environ.get("GEMINI_API_KEY", "")
    if provider == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY") or ""
    return ""


def provider_name() -> str:
    """'gemini', 'anthropic' or 'none' (no usable key / explicitly disabled)."""
    if _override is not None:
        return _override.name
    load_dotenv()
    want = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if want == "none":
        return "none"
    if want in ("gemini", "anthropic"):
        return want if _key(want) else "none"
    for p in ("gemini", "anthropic"):
        if _key(p):
            return p
    return "none"


def get_provider():
    name = provider_name()
    if _override is not None:
        return _override
    if name == "none":
        return None
    if name not in _instances:
        _instances[name] = (GeminiProvider if name == "gemini" else AnthropicProvider)(_key(name))
    return _instances[name]


def is_configured() -> bool:
    return provider_name() != "none"


def model_for(role: str) -> str:
    """Model name that WILL be used for 'classifier' or 'vision' ('' when no provider)."""
    p = get_provider()
    return p.model_for(role) if p else ""


def describe() -> dict:
    load_dotenv()
    return {"provider": provider_name(),
            "key_present": {"gemini": bool(_key("gemini")), "anthropic": bool(_key("anthropic"))},
            "classifier_model": model_for("classifier"), "vision_model": model_for("vision")}


def _require():
    p = get_provider()
    if p is None:
        raise LLMFailure("no_key", "no LLM provider configured (set GEMINI_API_KEY or ANTHROPIC_API_KEY)")
    return p


def complete_json(system: str, user: str, schema: dict | None = None) -> tuple[dict, dict]:
    return _require().complete_json(system, user, schema)


def vision_json(system: str, prompt: str, image_bytes: bytes, mime: str, schema: dict) -> tuple[dict, dict]:
    return _require().vision_json(system, prompt, image_bytes, mime, schema)


def list_models(contains: str = "") -> list[str]:
    """Model names the configured key can see (Gemini only), optionally filtered by substring.
    Names only: no key, no other data. Raises LLMFailure on API errors."""
    p = _require()
    if getattr(p, "name", "") != "gemini":
        raise LLMFailure("other", "model listing is implemented for the gemini provider only")
    try:
        names = [str(getattr(m, "name", m)).removeprefix("models/") for m in p._get_client().models.list()]
    except Exception as e:
        kind, _ = classify_exception(e)
        raise LLMFailure(kind, _scrub(f"{type(e).__name__}: {str(e)[:200]}"))
    return sorted(n for n in names if contains.lower() in n.lower())


# 1x1 transparent PNG, used only by ping() to test a vision model with real bytes.
_PING_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
_PING_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def ping(role: str) -> tuple[bool, str, dict | None]:
    """Minimal real call that confirms the model for ``role`` exists and answers.
    Returns (ok, detail, usage). Costs a handful of tokens; used by tools/check_env.py --ping."""
    try:
        if role == "vision":
            _, usage = vision_json("Answer in JSON.", 'Reply with {"ok": true}.', _PING_PNG, "image/png", _PING_SCHEMA)
        else:
            _, usage = complete_json("Answer in JSON.", 'Reply with {"ok": true}.', _PING_SCHEMA)
        return True, f"{usage['provider']}/{usage['model']} ok ({usage['input_tokens']} in, {usage['output_tokens']} out)", usage
    except LLMFailure as f:
        return False, str(f), f.usage
