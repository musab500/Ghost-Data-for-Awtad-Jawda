"""Tests for llm_provider.py (Person B). No network: responses are built with the real
google-genai types and handed to a fake client; Anthropic uses duck-typed fakes.
Run from repo root:  python3 -m unittest discover -s code/tests -v
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import llm_provider as lp  # noqa: E402

try:
    from google.genai import errors as gerrors, types as gtypes
    HAS_GENAI = True
except ImportError:  # the Gemini SDK is optional for the rest of the project
    HAS_GENAI = False

SCHEMA = {"type": "object", "properties": {"is_deletion": {"type": "boolean"},
                                           "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
          "required": ["is_deletion", "confidence"]}
GOOD = '{"is_deletion": true, "confidence": 0.9}'


class FakeClock:
    def __init__(self):
        self.sleeps = []

    def __call__(self, s):
        self.sleeps.append(s)


class SchemaValidation(unittest.TestCase):
    def test_accepts_valid_and_rejects_invalid(self):
        self.assertEqual(lp.validate_schema({"is_deletion": True, "confidence": 0.5}, SCHEMA), [])
        self.assertTrue(lp.validate_schema({"is_deletion": "yes", "confidence": 0.5}, SCHEMA))
        self.assertTrue(lp.validate_schema({"is_deletion": True}, SCHEMA))                    # required
        self.assertTrue(lp.validate_schema({"is_deletion": True, "confidence": 3}, SCHEMA))   # maximum
        self.assertTrue(lp.validate_schema({"is_deletion": True, "confidence": True}, SCHEMA))  # bool is not a number

    def test_parse_json_text_failures(self):
        u = {"input_tokens": 1}
        for text, kind in [(None, "empty"), ("  ", "empty"), ("not json", "malformed"),
                           ("[1, 2]", "malformed"), ('{"is_deletion": 1, "confidence": 0.5}', "schema")]:
            with self.assertRaises(lp.LLMFailure) as cm:
                lp.parse_json_text(text, SCHEMA, u)
            self.assertEqual(cm.exception.kind, kind)
            self.assertIs(cm.exception.usage, u)
        self.assertEqual(lp.parse_json_text("```json\n" + GOOD + "\n```", SCHEMA, u)["confidence"], 0.9)


def gresp(text=GOOD, finish="STOP", pt=50, ct=10, tt=7, usage=True, block=None, model="gemini-test-001",
          candidates=True):
    um = gtypes.GenerateContentResponseUsageMetadata(
        prompt_token_count=pt, candidates_token_count=ct, thoughts_token_count=tt, total_token_count=pt + ct + tt) if usage else None
    cands = [gtypes.Candidate(content=gtypes.Content(role="model", parts=[gtypes.Part(text=text)]),
                              finish_reason=finish)] if candidates else None
    fb = gtypes.GenerateContentResponsePromptFeedback(block_reason=block) if block else None
    return gtypes.GenerateContentResponse(candidates=cands, usage_metadata=um, prompt_feedback=fb, model_version=model)


class FakeGenaiClient:
    """Plays back a list of responses / exceptions, recording every call."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.models = NS(generate_content=self._gen)

    def _gen(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@unittest.skipUnless(HAS_GENAI, "the `google-genai` package is not installed (pip install -r requirements.txt)")
class GeminiProviderTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch.object(lp, "_sleep", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for v in ("CLASSIFIER_MODEL", "VISION_MODEL"):
            os.environ.pop(v, None)

    def provider(self, script):
        self.client = FakeGenaiClient(script)
        return lp.GeminiProvider("k-test", client=self.client)

    def test_success_usage_comes_only_from_the_response(self):
        parsed, usage = self.provider([gresp()]).complete_json("sys", "hello", SCHEMA)
        self.assertEqual(parsed, {"is_deletion": True, "confidence": 0.9})
        self.assertEqual({k: usage[k] for k in ("provider", "model", "requested_model", "input_tokens", "output_tokens", "usage_reported")},
                         {"provider": "gemini", "model": "gemini-test-001",
                          "requested_model": lp.DEFAULT_MODELS["gemini"]["classifier"],
                          "input_tokens": 50, "output_tokens": 17, "usage_reported": True})  # 10 + 7 thinking
        self.assertEqual(len(usage["attempts"]), 1)
        self.assertFalse(usage["attempts"][0]["retry"])
        cfg = self.client.calls[0]["config"]
        self.assertEqual(cfg.response_mime_type, "application/json")
        self.assertEqual(cfg.response_json_schema, SCHEMA)
        self.assertEqual(self.client.calls[0]["model"], lp.DEFAULT_MODELS["gemini"]["classifier"])

    def test_vision_calls_turn_thinking_off_classifier_calls_do_not(self):
        self.provider([gresp(), gresp()])
        self.client.script = [gresp(), gresp()]
        p = lp.GeminiProvider("k-test", client=self.client)
        p.vision_json("s", "p", b"\x89PNG", "image/png", SCHEMA)
        p.complete_json("s", "u", SCHEMA)
        vision_cfg, classifier_cfg = (c["config"] for c in self.client.calls)
        self.assertEqual(vision_cfg.thinking_config.thinking_budget, 0)
        self.assertIsNone(classifier_cfg.thinking_config)
        self.assertEqual(vision_cfg.max_output_tokens, lp.MAX_OUTPUT_TOKENS["vision"])

    def test_vision_thinking_budget_env_and_unsupported_model_fallback(self):
        os.environ["VISION_THINKING_BUDGET"] = "128"
        self.addCleanup(os.environ.pop, "VISION_THINKING_BUDGET", None)
        p = self.provider([gresp()])
        p.vision_json("s", "p", b"x", "image/png", SCHEMA)
        self.assertEqual(self.client.calls[0]["config"].thinking_config.thinking_budget, 128)
        os.environ["VISION_THINKING_BUDGET"] = "0"
        p = self.provider([ValueError("400 INVALID_ARGUMENT: thinking budget is not supported"), gresp()])
        p.vision_json("s", "p", b"x", "image/png", SCHEMA)
        self.assertIsNotNone(self.client.calls[0]["config"].thinking_config)
        self.assertIsNone(self.client.calls[1]["config"].thinking_config)

    def test_model_env_override(self):
        os.environ["CLASSIFIER_MODEL"] = "gemini-custom"
        self.provider([gresp()]).complete_json("s", "u", SCHEMA)
        self.assertEqual(self.client.calls[0]["model"], "gemini-custom")

    def test_missing_usage_metadata_is_flagged_not_invented(self):
        _, usage = self.provider([gresp(usage=False)]).complete_json("s", "u", SCHEMA)
        self.assertFalse(usage["usage_reported"])
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (0, 0))

    def test_unusable_responses_raise_with_the_right_kind_and_keep_usage(self):
        cases = [
            (gresp(block=gtypes.BlockedReason.SAFETY, candidates=False), "blocked"),
            (gresp(finish=gtypes.FinishReason.SAFETY), "blocked"),
            (gresp(finish=gtypes.FinishReason.PROHIBITED_CONTENT), "blocked"),
            (gresp(candidates=False), "empty"),
            (gresp(text=""), "empty"),
            (gresp(text="sorry, I cannot"), "malformed"),
            (gresp(text='{"is_deletion": "maybe", "confidence": 0.5}'), "schema"),
        ]
        for resp, kind in cases:
            with self.subTest(kind=kind):
                with self.assertRaises(lp.LLMFailure) as cm:
                    self.provider([resp]).complete_json("s", "u", SCHEMA)
                self.assertEqual(cm.exception.kind, kind)
                self.assertEqual(cm.exception.usage["input_tokens"], 50)   # tokens were spent: kept for accounting
                self.assertEqual(len(self.client.calls), 1)                  # content failures are not retried

    def test_transient_error_is_retried_then_succeeds(self):
        srv = gerrors.ServerError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
        parsed, _ = self.provider([srv, srv, gresp()]).complete_json("s", "u", SCHEMA)
        self.assertTrue(parsed["is_deletion"])
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(self.clock.sleeps, [1.0, 3.0])

    def test_rate_limit_is_capped_and_reported_clearly(self):
        rl = gerrors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
        with self.assertRaises(lp.LLMFailure) as cm:
            self.provider([rl, rl, rl, gresp()]).complete_json("s", "u", SCHEMA)
        self.assertEqual(cm.exception.kind, "rate_limit")
        self.assertIn("429", cm.exception.message)
        self.assertEqual(len(self.client.calls), lp.MAX_ATTEMPTS)          # hard cap: the 4th item is never used

    def test_auth_and_not_found_are_not_retried(self):
        for code, status, kind in [(401, "UNAUTHENTICATED", "auth"), (403, "PERMISSION_DENIED", "auth"),
                                   (404, "NOT_FOUND", "not_found")]:
            err = gerrors.ClientError(code, {"error": {"code": code, "message": "x", "status": status}})
            with self.subTest(code=code):
                with self.assertRaises(lp.LLMFailure) as cm:
                    self.provider([err]).complete_json("s", "u", SCHEMA)
                self.assertEqual(cm.exception.kind, kind)
                self.assertEqual(len(self.client.calls), 1)
        self.assertIn("CLASSIFIER_MODEL", cm.exception.message)            # tells the user how to fix a bad model name

    def test_truncated_answer_is_retried_once_with_a_higher_limit(self):
        trunc = gresp(finish=gtypes.FinishReason.MAX_TOKENS, text='{"is_')
        parsed, usage = self.provider([trunc, gresp()]).complete_json("s", "u", SCHEMA)
        self.assertEqual(parsed["confidence"], 0.9)                       # from the SECOND response only
        limits = [c["config"].max_output_tokens for c in self.client.calls]
        self.assertEqual(limits, [lp.MAX_OUTPUT_TOKENS["classifier"], lp.RETRY_MAX_OUTPUT_TOKENS["classifier"]])
        self.assertGreater(limits[1], limits[0])
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (100, 34))   # both responses were billed
        first, second = usage["attempts"]
        self.assertEqual((first["ok"], first["failure"], first["retry"], first["max_output_tokens"]),
                         (False, "truncated", False, limits[0]))
        self.assertEqual((second["ok"], second["retry"], second["max_output_tokens"]), (True, True, limits[1]))

    def test_vision_limit_is_at_least_8192_and_retries_higher(self):
        self.assertGreaterEqual(lp.MAX_OUTPUT_TOKENS["vision"], 8192)
        trunc = gresp(finish=gtypes.FinishReason.MAX_TOKENS, text="{")
        self.provider([trunc, gresp()]).vision_json("s", "p", b"img", "image/png", SCHEMA)
        limits = [c["config"].max_output_tokens for c in self.client.calls]
        self.assertEqual(limits, [lp.MAX_OUTPUT_TOKENS["vision"], lp.RETRY_MAX_OUTPUT_TOKENS["vision"]])
        self.assertGreater(limits[1], limits[0])

    def test_truncated_twice_fails_after_exactly_one_retry_and_never_uses_the_partial_answer(self):
        # the truncated text is VALID JSON that satisfies the schema: it must still be rejected
        looks_valid = gresp(finish=gtypes.FinishReason.MAX_TOKENS, text=GOOD)
        with self.assertRaises(lp.LLMFailure) as cm:
            self.provider([looks_valid, looks_valid, gresp()]).complete_json("s", "u", SCHEMA)
        self.assertEqual(cm.exception.kind, "truncated")
        self.assertEqual(len(self.client.calls), 2)                        # one retry, never a third call
        self.assertEqual(cm.exception.usage["input_tokens"], 100)          # both responses accounted for
        self.assertEqual([a["failure"] for a in cm.exception.usage["attempts"]], ["truncated", "truncated"])

    def test_list_models_returns_filtered_names_only(self):
        c = NS(models=NS(list=lambda: [NS(name="models/gemini-3.5-flash-lite"), NS(name="models/gemini-3.6-flash"),
                                       NS(name="models/gemini-3.6-pro"), NS(name="models/embedding-001")]))
        lp.set_provider(lp.GeminiProvider("k-test", client=c))
        self.addCleanup(lp.reset_provider)
        self.assertEqual(lp.list_models("flash"), ["gemini-3.5-flash-lite", "gemini-3.6-flash"])
        lp.set_provider(lp.AnthropicProvider("k", client=None))
        with self.assertRaises(lp.LLMFailure):
            lp.list_models()

    def test_vision_sends_image_bytes_with_mime(self):
        self.provider([gresp()]).vision_json("sys", "read this", b"\x89PNGdata", "image/png", SCHEMA)
        contents = self.client.calls[0]["contents"]
        self.assertEqual(contents[0].inline_data.data, b"\x89PNGdata")
        self.assertEqual(contents[0].inline_data.mime_type, "image/png")
        self.assertEqual(contents[1], "read this")
        self.assertEqual(self.client.calls[0]["model"], lp.DEFAULT_MODELS["gemini"]["vision"])


class FakeAnthropicClient:
    """A single message/exception is returned every time; a list is played back in order."""

    def __init__(self, msg):
        self.msg, self.calls = msg, []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        item = self.msg.pop(0) if isinstance(self.msg, list) else self.msg
        if isinstance(item, Exception):
            raise item
        return item


def amsg(text=GOOD, stop="end_turn", tin=40, tout=12, model="claude-haiku-5-5"):
    return NS(content=[NS(text=text)], stop_reason=stop, usage=NS(input_tokens=tin, output_tokens=tout), model=model)


class AnthropicProviderTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(lp, "_sleep", FakeClock())
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {}, clear=False)
        e.start()
        self.addCleanup(e.stop)
        for v in ("CLASSIFIER_MODEL", "VISION_MODEL", "GHOST_CLASSIFIER_MODEL"):
            os.environ.pop(v, None)

    def test_success_and_same_interface(self):
        c = FakeAnthropicClient(amsg())
        parsed, usage = lp.AnthropicProvider("k", client=c).complete_json("sys", "hi", SCHEMA)
        self.assertEqual(parsed["confidence"], 0.9)
        self.assertEqual({k: usage[k] for k in ("provider", "model", "input_tokens", "output_tokens")},
                         {"provider": "anthropic", "model": "claude-haiku-5-5", "input_tokens": 40, "output_tokens": 12})
        self.assertIn("JSON Schema", c.calls[0]["system"])

    def test_refusal_is_a_failure_with_usage(self):
        for stop, kind in [("refusal", "blocked")]:
            with self.assertRaises(lp.LLMFailure) as cm:
                lp.AnthropicProvider("k", client=FakeAnthropicClient(amsg(stop=stop))).complete_json("s", "u", SCHEMA)
            self.assertEqual(cm.exception.kind, kind)
            self.assertEqual(cm.exception.usage["output_tokens"], 12)

    def test_truncation_is_retried_once_with_a_higher_max_tokens(self):
        c = FakeAnthropicClient([amsg(text='{"is_', stop="max_tokens"), amsg()])
        parsed, usage = lp.AnthropicProvider("k", client=c).complete_json("s", "u", SCHEMA)
        self.assertEqual(parsed["confidence"], 0.9)
        self.assertEqual([k["max_tokens"] for k in c.calls],
                         [lp.MAX_OUTPUT_TOKENS["classifier"], lp.RETRY_MAX_OUTPUT_TOKENS["classifier"]])
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (80, 24))
        self.assertEqual([a["ok"] for a in usage["attempts"]], [False, True])

    def test_truncated_twice_is_a_failure_with_both_attempts_recorded(self):
        c = FakeAnthropicClient([amsg(text=GOOD, stop="max_tokens"), amsg(text=GOOD, stop="max_tokens"), amsg()])
        with self.assertRaises(lp.LLMFailure) as cm:
            lp.AnthropicProvider("k", client=c).complete_json("s", "u", SCHEMA)
        self.assertEqual((cm.exception.kind, len(c.calls), len(cm.exception.usage["attempts"])), ("truncated", 2, 2))

    def test_vision_builds_an_image_block(self):
        c = FakeAnthropicClient(amsg())
        lp.AnthropicProvider("k", client=c).vision_json("s", "look", b"abc", "image/png", SCHEMA)
        block = c.calls[0]["messages"][0]["content"][0]
        self.assertEqual((block["type"], block["source"]["media_type"]), ("image", "image/png"))

    def test_status_code_errors_use_the_same_classification(self):
        class Boom(Exception):
            status_code = 429
        with self.assertRaises(lp.LLMFailure) as cm:
            lp.AnthropicProvider("k", client=FakeAnthropicClient(Boom("slow down"))).complete_json("s", "u", SCHEMA)
        self.assertEqual(cm.exception.kind, "rate_limit")


class Selection(unittest.TestCase):
    def setUp(self):
        lp.reset_provider()
        self.addCleanup(lp.reset_provider)
        p = mock.patch.object(lp, "load_dotenv", lambda path=None: False)   # never read a real .env
        p.start()
        self.addCleanup(p.stop)

    def env(self, **kw):
        base = {k: v for k, v in os.environ.items()
                if k not in ("LLM_PROVIDER", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY",
                             "CLASSIFIER_MODEL", "VISION_MODEL", "GHOST_CLASSIFIER_MODEL")}
        base.update(kw)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_default_prefers_gemini_then_anthropic_then_none(self):
        with self.env(GEMINI_API_KEY="g", ANTHROPIC_API_KEY="a"):
            self.assertEqual(lp.provider_name(), "gemini")
        with self.env(ANTHROPIC_API_KEY="a"):
            self.assertEqual(lp.provider_name(), "anthropic")
        with self.env():
            self.assertEqual(lp.provider_name(), "none")
            self.assertFalse(lp.is_configured())
            with self.assertRaises(lp.LLMFailure) as cm:
                lp.complete_json("s", "u")
            self.assertEqual(cm.exception.kind, "no_key")

    def test_explicit_provider_needs_its_own_key(self):
        with self.env(LLM_PROVIDER="anthropic", GEMINI_API_KEY="g"):
            self.assertEqual(lp.provider_name(), "none")
        with self.env(LLM_PROVIDER="anthropic", GEMINI_API_KEY="g", ANTHROPIC_API_KEY="a"):
            self.assertEqual(lp.provider_name(), "anthropic")
        with self.env(LLM_PROVIDER="none", GEMINI_API_KEY="g"):
            self.assertEqual(lp.provider_name(), "none")

    def test_describe_reports_names_and_presence_only(self):
        with self.env(GEMINI_API_KEY="super-secret-value", CLASSIFIER_MODEL="m1"):
            d = lp.describe()
        self.assertEqual((d["provider"], d["classifier_model"]), ("gemini", "m1"))
        self.assertEqual(d["key_present"], {"gemini": True, "anthropic": False})
        self.assertNotIn("super-secret-value", repr(d))

    def test_error_messages_never_contain_the_key(self):
        class Boom(Exception):
            code = 400
        key = "AIzaSy-FAKE-KEY-12345"   # shorter than a real key on purpose
        with self.env(GEMINI_API_KEY=key):
            with self.assertRaises(lp.LLMFailure) as cm:
                lp._with_retries(lambda: (_ for _ in ()).throw(Boom(f"bad request for key {key}")), "m")
        self.assertNotIn(key, str(cm.exception))
        self.assertIn("[REDACTED]", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
