"""Tests for multimodal.py (Person A). No network: the vision API is never
called because ANTHROPIC_API_KEY is removed for these tests."""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
DATASET = CODE.parent / "dataset"

import multimodal  # noqa: E402
from multimodal import multimodal_observations, records_to_observations  # noqa: E402
from parsers import load_evidence  # noqa: E402
from parsers.activity import parse_activity, resource_aliases  # noqa: E402
from parsers.manifest import load_manifest  # noqa: E402


class Multimodal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._key = os.environ.pop("ANTHROPIC_API_KEY", None)
        _o, _s, cls.skipped = load_evidence(DATASET)
        events, _a, _l = parse_activity(DATASET)
        cls.aliases = resource_aliases(events)
        cls.obs = multimodal_observations(cls.skipped, cls.aliases, log=io.StringIO())

    @classmethod
    def tearDownClass(cls):
        if cls._key is not None:
            os.environ["ANTHROPIC_API_KEY"] = cls._key

    def by(self, rid):
        return [o for o in self.obs if o.resource_id == rid]

    def test_all_skipped_files_are_processed(self):
        self.assertEqual({e.kind for e in self.skipped}, {"png", "pdf"})
        self.assertEqual({o.evidence_id for o in self.obs} - {e.file_id for e in self.skipped}, set())

    def test_pdf_support_attachment_for_case_07(self):
        o, = self.by("support_001")
        self.assertEqual((o.system_id, o.category, o.state, o.evidence_id, o.source_kind),
                         ("support_platform", "SUPPORT_ATTACHMENT", "FOUND", "evidence_58", "pdf"))
        self.assertIn("priya.raman@gmail.com", o.identifiers)
        self.assertIn("+919876573142", o.identifiers)

    def test_pdf_retention_date_and_alias(self):
        o, = [x for x in self.by("audit_record_003") if x.source_kind == "pdf"]   # also in evidence_37
        self.assertEqual(o.raw["retention_until"], "2027-08-25T04:03:00Z")
        o, = self.by("export_1842")          # statement number NST-2026-0718-1842
        self.assertEqual((o.category, o.evidence_id), ("USER_EXPORT", "evidence_18"))

    def test_register_without_resource_ids_emits_nothing(self):
        self.assertFalse([o for o in self.obs if o.evidence_id == "evidence_27"])

    def test_image_aliases_resolve_and_unnamed_rows_drop(self):
        o, = self.by("object_19")            # delivery_confirmation_19.pdf
        self.assertEqual((o.system_id, o.category), ("object_storage", "SUPPORT_ATTACHMENT"))
        self.assertFalse(self.by("delivery_confirmation_20.pdf"))
        o, = self.by("tombstone_17")         # customer key user_17
        self.assertEqual(o.state, "REDACTED")

    def test_observed_at_never_after_capture(self):
        from models import parse_ts
        for o in self.obs:
            self.assertLessEqual(parse_ts(o.observed_at), parse_ts(o.captured_at), o)

    def test_validation_drops_bad_records(self):
        ev = next(e for e in load_manifest(DATASET) if e.file_id == "evidence_04")
        log = io.StringIO()
        out = records_to_observations(ev, [
            {"keys": ["account_1"], "state": "MAYBE", "category": "USER_PROFILE", "identifiers": []},
            {"keys": ["nothing here"], "state": "FOUND", "category": "USER_PROFILE", "identifiers": []},
            {"keys": ["account_2"], "state": "FOUND", "category": "NOT_A_CATEGORY", "identifiers": []},
            {"keys": ["account_3"], "state": "found", "category": "USER_PROFILE", "identifiers": ["A@B.co"]},
        ], {}, log)
        self.assertEqual([(o.resource_id, o.state, o.identifiers) for o in out],
                         [("account_3", "FOUND", {"a@b.co"})])
        self.assertEqual(log.getvalue().count("dropped"), 3)

    def test_main_py_hook_call_shape(self):
        # main.py calls multimodal_observations(skipped, dataset_dir)
        out = multimodal_observations(self.skipped, DATASET, log=io.StringIO(), backend=None,
                                      usage_path=Path(tempfile.mkdtemp()) / "u.json")
        self.assertIn("object_19", {o.resource_id for o in out})   # needs the catalog aliases


# ---------------------------------------------------------------------------
# Vision path with a FAKE provider. Nothing here talks to any model or SDK.
# ---------------------------------------------------------------------------
class FakeFailure(Exception):
    """Shaped like llm_provider.LLMFailure: .kind and (optionally) .usage."""

    def __init__(self, kind, message="fake failure", usage=None):
        super().__init__(f"[{kind}] {message}")
        self.kind, self.usage = kind, usage


GOOD = {"records": [{
    "resource_id": "support_1034", "keys": ["CS-5034"], "system_id": "support_platform",
    "category": "SUPPORT_MESSAGE", "state": "IDENTIFIABLE", "identifiers": ["Kagyeman8947@fastmail.com"],
    "observed_at": "", "notes": "fake"}]}


class FakeProvider:
    """Records what it is called with; returns a canned result or raises."""

    def __init__(self, provider="fakeprov", model="fake-model-1", payload=GOOD, fail=None, served=None):
        self.provider, self.model, self.payload, self.fail = provider, model, payload, fail
        self.served = served or model
        self.calls = []

    def call(self, system, prompt, image_bytes, mime, schema):
        self.calls.append({"system": system, "prompt": prompt, "bytes": image_bytes, "mime": mime, "schema": schema})
        if self.fail:
            raise self.fail
        return self.payload, {"provider": self.provider, "model": self.served, "requested_model": self.model,
                              "input_tokens": 1500, "output_tokens": 321, "usage_reported": True}

    def backend(self):
        return multimodal.VisionBackend(self.provider, self.model, self.call)


class VisionViaBackend(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("MULTIMODAL_ALLOW_MANUAL", None)
        self.tmp = Path(tempfile.mkdtemp())
        self.cache, self.frozen, self.manual = self.tmp / "cache", self.tmp / "frozen", self.tmp / "manual"
        _o, _s, skipped = load_evidence(DATASET)
        self.ev = next(e for e in skipped if e.file_id == "evidence_28")      # support ticket PNG
        self.aliases = resource_aliases(parse_activity(DATASET)[0])

    def tearDown(self):
        os.environ.pop("MULTIMODAL_ALLOW_MANUAL", None)
        if self.saved is not None:
            os.environ["MULTIMODAL_ALLOW_MANUAL"] = self.saved

    def run_once(self, backend, files=None):
        log = io.StringIO()
        out = multimodal_observations(files or [self.ev], self.aliases, cache_dir=self.cache, log=log,
                                      manual_dir=self.manual, frozen_dir=self.frozen, backend=backend,
                                      usage_path=self.tmp / "usage.json")
        return out, log.getvalue(), json.loads((self.tmp / "usage.json").read_text())

    def put_manual(self, state="FOUND"):
        self.manual.mkdir(exist_ok=True)
        e = {"file_id": self.ev.file_id, "file_sha256": multimodal._sha256(self.ev.path),
             "extractor": {"method": "manual_transcription", "model": "by hand", "prompt_version": "manual-1"},
             "usage": None, "records": [{**GOOD["records"][0], "state": state}]}
        (self.manual / f"{e['file_sha256'][:16]}__manual.json").write_text(json.dumps(e))

    def put_entry(self, directory, provider, model, state, usage=True, name=None):
        directory.mkdir(exist_ok=True)
        e = {"file_id": self.ev.file_id, "file_sha256": multimodal._sha256(self.ev.path),
             "extractor": {"method": "vlm", "provider": provider, "model": model, "served_model": model,
                           "prompt_version": multimodal.PROMPT_VERSION},
             "usage": ({"provider": provider, "model": model, "input_tokens": 11, "output_tokens": 7,
                        "usage_reported": True, "at": "2026-10-25T10:00:00Z"} if usage else None),
             "records": [{**GOOD["records"][0], "state": state}]}
        key = multimodal._cache_key(self.ev.path, provider, model)
        (directory / (name or f"{key}.json")).write_text(json.dumps(e))
        return e

    # ---- the call and the ledger
    def test_call_contract_cache_key_and_ledger(self):
        fp = FakeProvider()
        out, log, usage = self.run_once(fp.backend())
        c, = fp.calls
        self.assertEqual((c["mime"], c["bytes"]), ("image/png", self.ev.path.read_bytes()))
        self.assertEqual(c["prompt"], multimodal.VISION_PROMPT)
        self.assertEqual(c["schema"], multimodal.RECORD_SCHEMA)
        item = c["schema"]["properties"]["records"]["items"]["properties"]
        from models import CATEGORIES, STATES
        self.assertEqual((item["state"]["enum"], item["category"]["enum"]), (sorted(STATES), sorted(CATEGORIES)))
        key = hashlib.sha256(self.ev.path.read_bytes() + f"\x00fakeprov\x00fake-model-1\x00{multimodal.PROMPT_VERSION}".encode()).hexdigest()
        self.assertTrue((self.cache / f"{key}.json").exists())             # sha256(bytes+provider+model+prompt)
        o, = out
        self.assertEqual((o.resource_id, o.state, o.source_kind, o.evidence_id), ("support_1034", "IDENTIFIABLE", "image", "evidence_28"))
        self.assertEqual(o.identifiers, {"kagyeman8947@fastmail.com"})
        self.assertEqual(o.raw["method"], "vlm:fakeprov/fake-model-1")
        call, = usage["calls"]
        self.assertEqual({k: call[k] for k in ("provider", "model", "input_tokens", "output_tokens", "ok", "from_cache")},
                         {"provider": "fakeprov", "model": "fake-model-1", "input_tokens": 1500, "output_tokens": 321,
                          "ok": True, "from_cache": False})
        self.assertEqual((usage["api_calls_this_run"], usage["input_tokens_this_run"], usage["output_tokens_this_run"]), (1, 1500, 321))

    def test_second_run_makes_zero_calls_and_ledger_keeps_the_original_call(self):
        fp = FakeProvider()
        self.run_once(fp.backend())
        out, _, usage = self.run_once(fp.backend())
        self.assertEqual(len(fp.calls), 1)
        self.assertEqual([o.resource_id for o in out], ["support_1034"])
        self.assertEqual(usage["api_calls_this_run"], 0)
        call, = usage["calls"]                                     # the real call behind the result used
        self.assertEqual((call["from_cache"], call["input_tokens"], call["output_tokens"]), (True, 1500, 321))

    def test_a_different_model_is_a_different_cache_entry(self):
        a, b = FakeProvider(model="m-a"), FakeProvider(model="m-b")
        self.run_once(a.backend())
        self.run_once(b.backend())
        self.assertEqual((len(a.calls), len(b.calls)), (1, 1))
        self.assertEqual(len(list(self.cache.glob("*.json"))), 2)

    # ---- failures never raise and are never cached
    def test_failure_with_usage_is_recorded_not_cached_and_does_not_raise(self):
        spent = {"provider": "fakeprov", "model": "fake-model-1", "input_tokens": 900, "output_tokens": 55, "usage_reported": True}
        out, log, usage = self.run_once(FakeProvider(fail=FakeFailure("truncated", "hit max tokens", spent)).backend())
        self.assertEqual(out, [])
        self.assertIn("[truncated]", log)
        self.assertFalse(self.cache.exists() and list(self.cache.glob("*.json")))
        call, = usage["calls"]
        self.assertEqual((call["ok"], call["input_tokens"], call["output_tokens"], call["error_kind"]), (False, 900, 55, "truncated"))

    def test_plain_exception_and_bad_payload_are_failures_too(self):
        for fp in (FakeProvider(fail=RuntimeError("boom")), FakeProvider(payload={"nope": 1}),
                   FakeProvider(payload={"records": "not a list"})):
            out, log, usage = self.run_once(fp.backend())
            self.assertEqual(out, [])
            self.assertIn("vision call failed", log)
            self.assertEqual((usage["calls"][0]["ok"], usage["calls"][0]["input_tokens"]), (False, 0))

    # ---- lookup order: cache > frozen > live call > manual
    def test_frozen_result_wins_over_a_live_call_whatever_model_made_it(self):
        self.put_entry(self.frozen, "other", "other-model", "REDACTED", name="frozen.json")
        fp = FakeProvider()
        out, _, usage = self.run_once(fp.backend())
        self.assertEqual(fp.calls, [])
        self.assertEqual(out[0].state, "REDACTED")
        self.assertEqual((usage["calls"][0]["provider"], usage["calls"][0]["from_cache"]), ("other", True))

    def test_exact_live_cache_beats_frozen(self):
        self.put_entry(self.frozen, "fakeprov", "fake-model-1", "REDACTED", name="frozen.json")
        self.put_entry(self.cache, "fakeprov", "fake-model-1", "IDENTIFIABLE")
        out, _, _ = self.run_once(FakeProvider().backend())
        self.assertEqual(out[0].state, "IDENTIFIABLE")

    def test_live_call_is_tried_before_the_manual_fallback_and_manual_is_the_fallback(self):
        self.put_manual("FOUND")
        out, _, _ = self.run_once(FakeProvider().backend())
        self.assertEqual(out[0].state, "IDENTIFIABLE")                       # model result, not the manual one
        shutil.rmtree(self.cache)
        out, log, _ = self.run_once(FakeProvider(fail=FakeFailure("rate_limit")).backend())
        self.assertEqual((out[0].state, out[0].raw["method"]), ("FOUND", "manual_transcription"))
        self.assertIn("using the manual transcription", log)

    def test_manual_fallback_can_be_forbidden(self):
        self.put_manual()
        os.environ["MULTIMODAL_ALLOW_MANUAL"] = "0"
        out, log, usage = self.run_once(None)
        self.assertEqual(out, [])
        self.assertEqual(usage["files_skipped"][0]["file_id"], "evidence_28")
        self.assertIn("not processed", log)

    def test_no_provider_uses_any_frozen_or_cached_result_and_otherwise_skips(self):
        out, log, usage = self.run_once(None)
        self.assertEqual((out, usage["api_calls_this_run"], usage["calls"]), ([], 0, []))
        self.assertIn("no provider configured", log)
        self.put_entry(self.cache, "somebody", "some-model", "FOUND")
        out, _, _ = self.run_once(None)
        self.assertEqual(out[0].state, "FOUND")

    def test_results_with_another_prompt_version_are_ignored(self):
        e = self.put_entry(self.frozen, "fakeprov", "fake-model-1", "REDACTED", name="old.json")
        e["extractor"]["prompt_version"] = "v1"
        (self.frozen / "old.json").write_text(json.dumps(e))
        fp = FakeProvider()
        out, _, _ = self.run_once(fp.backend())
        self.assertEqual(len(fp.calls), 1)

    def test_default_backend_is_none_without_a_configured_provider(self):
        try:
            import llm_provider as llm
        except ImportError:
            self.assertIsNone(multimodal.default_backend())
            return
        saved = {k: os.environ.get(k) for k in ("LLM_PROVIDER",)}
        os.environ["LLM_PROVIDER"] = "none"
        llm.reset_provider()
        try:
            self.assertIsNone(multimodal.default_backend())
        finally:
            os.environ.pop("LLM_PROVIDER", None)
            if saved["LLM_PROVIDER"] is not None:
                os.environ["LLM_PROVIDER"] = saved["LLM_PROVIDER"]
            llm.reset_provider()


class _ProviderLayer(unittest.TestCase):
    """Wiring through Person B's real llm_provider module, with a fake provider installed."""

    def test_default_backend_routes_to_llm_provider_vision_json(self):
        try:
            import llm_provider as llm
        except ImportError:
            self.skipTest("llm_provider.py not present in this checkout yet (Person B)")

        class Prov:
            name, seen = "fakegem", []

            def model_for(self, role):
                return "fake-vision-9"

            def complete_json(self, *a):
                raise AssertionError("not used")

            def vision_json(self, system, prompt, image_bytes, mime, schema):
                Prov.seen.append((mime, len(image_bytes)))
                if mime == "application/fail":
                    raise llm.LLMFailure("rate_limit", "slow down", {"provider": "fakegem", "model": "fake-vision-9",
                                                                     "input_tokens": 5, "output_tokens": 0})
                return GOOD, {"provider": "fakegem", "model": "fake-vision-9-001", "requested_model": "fake-vision-9",
                              "input_tokens": 10, "output_tokens": 4, "usage_reported": True}

        llm.set_provider(Prov())
        try:
            b = multimodal.default_backend()
            self.assertEqual((b.provider, b.model), ("fakegem", "fake-vision-9"))
            parsed, usage = b.call("s", "p", b"png-bytes", "image/png", {})
            self.assertEqual((parsed, usage["input_tokens"], Prov.seen), (GOOD, 10, [("image/png", 9)]))
            # a real LLMFailure (with usage) goes through the ledger path without raising
            ev = next(e for e in load_evidence(DATASET)[2] if e.file_id == "evidence_28")
            fresh, log = [], io.StringIO()
            bad = multimodal.VisionBackend("fakegem", "fake-vision-9",
                                           lambda s, p, i, m, sc: b.call(s, p, i, "application/fail", sc))
            self.assertIsNone(multimodal._call_vision(ev, bad, Path(tempfile.mkdtemp()), fresh, log))
            self.assertEqual((fresh[0]["ok"], fresh[0]["input_tokens"], fresh[0]["error_kind"]), (False, 5, "rate_limit"))
        finally:
            llm.reset_provider()


def _compare_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("compare_multimodal", CODE.parent / "tools" / "compare_multimodal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rec(rid, state="FOUND", ids=(), observed="", system="search_service", cat="SEARCH_INDEX_ENTRY"):
    return {"resource_id": rid, "keys": [], "system_id": system, "category": cat, "state": state,
            "identifiers": list(ids), "observed_at": observed, "notes": ""}


class CompareTool(unittest.TestCase):
    """Uses SYNTHETIC records only, to test the comparison logic."""

    @classmethod
    def setUpClass(cls):
        cls.tool = _compare_tool()

    def test_matched_missing_extra_and_field_mismatches(self):
        manual = [_rec("search_1", ids=["A@x.com"]), _rec("search_2", state="FOUND"),
                  _rec("search_3"), _rec("search_4", state="BOGUS")]
        vlm = [_rec("search_1", ids=["a@x.com"]),                 # identical after normalisation
               _rec("search_2", state="NOT_FOUND"),               # state differs
               _rec("search_9"),                                  # extra
               {**_rec("search_10"), "resource_id": "", "keys": ["no id here"]}]   # unresolvable
        c = self.tool.compare_records(manual, vlm, {})
        self.assertEqual((c["manual_n"], c["vlm_n"]), (3, 3))
        self.assertEqual(c["matched"], ["search_1", "search_2"])
        self.assertEqual((c["missing"], c["extra"]), (["search_3"], ["search_9"]))
        self.assertEqual([(x["resource_id"], x["field"]) for x in c["mismatches"]], [("search_2", "state")])
        self.assertEqual(len(c["manual_dropped"]), 1)
        self.assertEqual(len(c["vlm_dropped"]), 1)
        self.assertFalse(c["pipeline_identical"])

    def test_observed_at_only_difference_is_not_pipeline_relevant(self):
        c = self.tool.compare_records([_rec("search_1", observed="")], [_rec("search_1", observed="2026-01-01T00:00:00Z")], {})
        self.assertEqual([x["field"] for x in c["mismatches"]], ["observed_at"])
        self.assertTrue(c["pipeline_identical"])

    def test_report_and_freeze_use_only_model_results(self):
        mm, tool = multimodal, self.tool
        ev = next(e for e in load_evidence(DATASET)[2] if e.file_id == "evidence_28")
        with tempfile.TemporaryDirectory() as d:
            cache, frozen = Path(d) / "cache", Path(d) / "frozen"
            cache.mkdir()
            manual = tool._manual_entry(ev, mm.MANUAL_DIR)
            entry = {"file_id": ev.file_id, "file_sha256": mm._sha256(ev.path),
                     "extractor": {"method": "vlm", "provider": "fakeprov", "model": "m", "served_model": "m",
                                   "prompt_version": mm.PROMPT_VERSION},
                     "usage": {"input_tokens": 1, "output_tokens": 1},
                     "records": manual["records"]}                  # synthetic: a copy of the manual one
            (cache / "x.json").write_text(json.dumps(entry))
            text, tot = tool.build_report(DATASET, cache, frozen, mm.MANUAL_DIR)
            self.assertEqual((tot["compared"], tot["files"], tot["identical"]), (1, 11, 1))
            self.assertIn("no VLM result", text)
            self.assertEqual(tool.freeze(DATASET, cache, frozen), 1)
            self.assertEqual(len(list(frozen.glob("*.json"))), 1)
            # a manual transcription is never reported as a model result
            self.assertIsNone(tool._vlm_entry(ev, Path(d) / "none", Path(d) / "none2"))


if __name__ == "__main__":
    unittest.main()
