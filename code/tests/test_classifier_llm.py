"""Classifier on top of llm_provider, using a fake provider (no network). Person B.
Run from repo root:  python3 -m unittest discover -s code/tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import classifier  # noqa: E402
import llm_provider as lp  # noqa: E402

DELETE = "Please delete my account and erase all of my data."
NEGATED = "Please stop the promotional emails. I still need the account, so I am not asking for deletion."


def usage(tin=30, tout=9, model="fake-model-1"):
    return {"provider": "fakeprov", "model": model, "requested_model": model,
            "input_tokens": tin, "output_tokens": tout, "usage_reported": True}


class FakeProvider:
    name = "fakeprov"

    def __init__(self, script=None, model="fake-model-1"):
        self.script, self.model, self.calls = list(script or []), model, []

    def model_for(self, role):
        return self.model

    def complete_json(self, system, user, schema=None):
        self.calls.append(user)
        item = self.script.pop(0) if self.script else ({"is_deletion": True, "confidence": 0.9, "reason": "r"}, usage())
        if isinstance(item, Exception):
            raise item
        return item

    def vision_json(self, *a, **k):
        raise AssertionError("not used")


def ok(is_del=True, conf=0.9, **kw):
    return ({"is_deletion": is_del, "confidence": conf, "reason": "because"}, usage(**kw))


class ClassifierWithFakeProvider(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        for name, val in (("_CACHE_DIR", tmp), ("_CACHE_PATH", tmp / "classifier.json"),
                          ("USAGE_PATH", tmp / "classifier_usage.json")):
            p = mock.patch.object(classifier, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tmp
        p = mock.patch.object(lp, "load_dotenv", lambda path=None: False)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lp.reset_provider)

    def clf(self, provider):
        lp.set_provider(provider)
        return classifier.RequestClassifier()

    def usage_file(self):
        f = self.tmp / "classifier_usage.json"
        return json.loads(f.read_text()) if f.exists() else []

    def cache_file(self):
        f = self.tmp / "classifier.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def test_success_is_cached_and_usage_recorded_from_the_response(self):
        fp = FakeProvider([ok(tin=31, tout=7)])
        c = self.clf(fp)
        self.assertTrue(c.use_llm)
        self.assertTrue(c.llm_verdict(DELETE).is_deletion)
        rec = self.usage_file()
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]["provider"], rec[0]["model"], rec[0]["input_tokens"], rec[0]["output_tokens"], rec[0]["ok"]),
                         ("fakeprov", "fake-model-1", 31, 7, True))
        c2 = self.clf(FakeProvider([]))                      # a new run: served from the cache, no call
        c2.llm_verdict(DELETE)
        self.assertEqual((c2.cache_hits, c2.calls, len(self.usage_file())), (1, 0, 1))

    def test_cache_key_includes_provider_and_model(self):
        self.clf(FakeProvider([ok()])).llm_verdict(DELETE)
        other_model = self.clf(FakeProvider([ok()], model="fake-model-2"))
        other_model.llm_verdict(DELETE)
        self.assertEqual(other_model.calls, 1)               # different model -> not a cache hit
        self.assertEqual(len(self.cache_file()), 2)

    def test_blocked_response_is_skipped_not_cached_and_its_tokens_are_kept(self):
        fp = FakeProvider([lp.LLMFailure("blocked", "prompt blocked: SAFETY", usage(tin=25, tout=0)), ok(False)])
        c = self.clf(fp)
        self.assertIsNone(c.llm_verdict(DELETE))
        self.assertEqual(self.cache_file(), {})              # a failure is never cached as success
        rec = self.usage_file()
        self.assertEqual((rec[0]["ok"], rec[0]["failure"], rec[0]["input_tokens"]), (False, "blocked", 25))
        self.assertTrue(c.is_deletion(DELETE))               # run continues: fallback rules decide this text
        self.assertTrue(c.is_deletion(DELETE))
        self.assertEqual(len(fp.calls), 1)                   # a failed text is not re-sent within the run
        self.assertEqual(len(self.usage_file()), 1)
        nxt = self.clf(fp)                                   # the next run asks the model again
        again = nxt.llm_verdict(DELETE)
        self.assertEqual(len(fp.calls), 2)
        self.assertFalse(again.is_deletion)
        self.assertEqual(len(self.cache_file()), 1)          # cached only once it succeeded

    def test_every_truncation_retry_is_its_own_usage_record(self):
        trunc = {"provider": "fakeprov", "model": "fake-model-1", "requested_model": "fake-model-1", "input_tokens": 30,
                 "output_tokens": 2048, "usage_reported": True, "ok": False, "failure": "truncated",
                 "max_output_tokens": 2048, "retry": False}
        good = {**trunc, "output_tokens": 9, "ok": True, "failure": "", "max_output_tokens": 4096, "retry": True}
        merged = {**good, "input_tokens": 60, "output_tokens": 2057, "attempts": [trunc, good]}
        c = self.clf(FakeProvider([({"is_deletion": True, "confidence": 0.9, "reason": "r"}, merged)]))
        self.assertTrue(c.llm_verdict(DELETE).is_deletion)
        recs = self.usage_file()
        self.assertEqual([(r["ok"], r["failure"], r["retry"], r["max_output_tokens"]) for r in recs],
                         [(False, "truncated", False, 2048), (True, "", True, 4096)])
        self.assertEqual((c.calls, c.input_tokens, c.output_tokens), (2, 60, 2057))
        self.assertEqual(len(self.cache_file()), 1)

    def test_a_truncated_failure_records_both_attempts_and_caches_nothing(self):
        a1 = {**usage(), "ok": False, "failure": "truncated", "max_output_tokens": 2048, "retry": False}
        a2 = {**usage(), "ok": False, "failure": "truncated", "max_output_tokens": 4096, "retry": True}
        fail = lp.LLMFailure("truncated", "cut off twice", {**usage(60, 4096), "attempts": [a1, a2]})
        c = self.clf(FakeProvider([fail]))
        self.assertIsNone(c.llm_verdict(DELETE))
        self.assertEqual([(r["ok"], r["retry"]) for r in self.usage_file()], [(False, False), (False, True)])
        self.assertEqual(self.cache_file(), {})

    def test_failure_without_a_response_records_no_usage(self):
        c = self.clf(FakeProvider([lp.LLMFailure("transient", "still failing after 3 attempts")]))
        self.assertIsNone(c.llm_verdict(DELETE))
        self.assertEqual(self.usage_file(), [])
        self.assertEqual(self.cache_file(), {})

    def test_malformed_and_unexpected_errors_never_crash(self):
        c = self.clf(FakeProvider([lp.LLMFailure("malformed", "not JSON", usage()), RuntimeError("boom")]))
        self.assertIsNone(c.llm_verdict("text one"))
        self.assertIsNone(c.llm_verdict("text two"))
        self.assertEqual(self.cache_file(), {})
        self.assertEqual(len(c.llm_errors), 2)

    def test_negation_override_wins_over_the_llm(self):
        c = self.clf(FakeProvider([ok(True, 0.99)]))
        self.assertTrue(c.llm_verdict(NEGATED).is_deletion)   # LLM alone is wrong here
        r = c.classify(NEGATED)                               # combined: served from cache, override applied
        self.assertFalse(r.is_deletion)
        self.assertEqual(r.source, "llm+override")

    def test_repeated_hard_failures_switch_the_llm_off_for_the_run(self):
        err = lambda: lp.LLMFailure("auth", "check the API key")  # noqa: E731
        fp = FakeProvider([err(), err(), err(), ok()])
        c = self.clf(fp)
        for t in ("a", "b", "c", "d"):
            self.assertIsNone(c.llm_verdict(t))
        self.assertEqual(len(fp.calls), classifier.HARD_FAILURE_LIMIT)   # the 4th text never reached the provider
        self.assertIn("consecutive 'auth'", c.llm_disabled_reason)

    def test_classifier_loads_dotenv_through_the_module_so_tests_never_read_a_real_env_file(self):
        calls = []
        with mock.patch.object(lp, "load_dotenv", lambda path=None: calls.append(1) or False):
            classifier.RequestClassifier(use_llm=False)
        self.assertEqual(calls, [1])      # regression: a developer's real .env once leaked into a test

    def test_no_provider_means_fallback_with_no_calls(self):
        lp.reset_provider()
        with mock.patch.dict("os.environ", {}, clear=True):
            c = classifier.RequestClassifier()
            self.assertFalse(c.use_llm)
            self.assertIsNone(c.llm_verdict(DELETE))
            self.assertTrue(c.is_deletion(DELETE))
            self.assertFalse(c.is_deletion(NEGATED))


if __name__ == "__main__":
    unittest.main()
