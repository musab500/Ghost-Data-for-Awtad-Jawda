"""Tests for multimodal.py (Person A). No network: the vision API is never
called because ANTHROPIC_API_KEY is removed for these tests."""
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

    def test_no_key_and_no_cache_skips_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            log = io.StringIO()
            png = [e for e in self.skipped if e.kind == "png"][:1]
            out = multimodal_observations(png, self.aliases, cache_dir=Path(d), log=log,
                                          manual_dir=Path(d) / "none")
            self.assertEqual(out, [])
            self.assertIn("not processed", log.getvalue())
            usage = (Path(d) / "usage.json").read_text()
            self.assertIn('"api_calls_count": 0', usage)

    def test_cached_api_result_becomes_observation(self):
        # A cached API result (same key the real call would write) is used
        # without any API call, and its fields map onto the Observation.
        ev = next(e for e in self.skipped if e.file_id == "evidence_28")   # support ticket PNG
        cached = {"file_id": ev.file_id, "file_sha256": multimodal._sha256(ev.path),
                  "extractor": {"method": "api", "model": multimodal._model(),
                                "prompt_version": multimodal.PROMPT_VERSION},
                  "usage": {"input_tokens": 1234, "output_tokens": 56},
                  "records": [{"resource_id": "support_1034", "keys": ["CS-5034"],
                               "system_id": "support_platform", "category": "SUPPORT_MESSAGE",
                               "state": "IDENTIFIABLE", "identifiers": ["KAgyeman8947@fastmail.com"],
                               "observed_at": "2026-05-24T06:59:00Z", "notes": "ticket"}]}
        with tempfile.TemporaryDirectory() as d:
            key = multimodal._cache_key(ev.path, multimodal._model())
            (Path(d) / f"{key}.json").write_text(json.dumps(cached))
            out = multimodal_observations([ev], self.aliases, cache_dir=Path(d), log=io.StringIO(),
                                          manual_dir=Path(d) / "none")
            usage = json.loads((Path(d) / "usage.json").read_text())
        o, = out
        self.assertEqual((o.resource_id, o.system_id, o.category, o.state, o.source_kind),
                         ("support_1034", "support_platform", "SUPPORT_MESSAGE", "IDENTIFIABLE", "image"))
        self.assertEqual((o.evidence_id, o.captured_at), ("evidence_28", ev.captured_at))
        self.assertEqual(o.identifiers, {"kagyeman8947@fastmail.com"})
        self.assertEqual(o.observed_at, "2026-05-24T06:59:00Z")
        self.assertEqual(usage["api_calls_count"], 0)
        self.assertEqual(usage["files_used"][0]["usage"], {"input_tokens": 1234, "output_tokens": 56})

    def test_cache_key_depends_on_model_and_prompt(self):
        ev = self.skipped[0]
        self.assertNotEqual(multimodal._cache_key(ev.path, "model-a"), multimodal._cache_key(ev.path, "model-b"))

    def test_main_py_hook_call_shape(self):
        # main.py calls multimodal_observations(skipped, dataset_dir)
        out = multimodal_observations(self.skipped, DATASET, log=io.StringIO(),
                                      usage_path=Path(tempfile.mkdtemp()) / "u.json")
        self.assertIn("object_19", {o.resource_id for o in out})   # needs the catalog aliases

    def test_manual_entries_can_be_disabled(self):
        os.environ["MULTIMODAL_ALLOW_MANUAL"] = "0"
        try:
            with tempfile.TemporaryDirectory() as d:
                png = [e for e in self.skipped if e.kind == "png"]
                out = multimodal_observations(png, self.aliases, cache_dir=Path(d), log=io.StringIO())
                self.assertEqual(out, [])
        finally:
            del os.environ["MULTIMODAL_ALLOW_MANUAL"]


class _StubAPI:
    """A local HTTP server standing in for the Messages API. Records every
    request it receives. NOT the real API: it only lets the real SDK run."""

    def __init__(self, status=200, body=None):
        import http.server
        import threading
        self.requests, self.status = [], status
        self.body = body
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "json": json.loads(raw)})
                payload = json.dumps(outer.body).encode()
                self.send_response(outer.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _message(text, stop="end_turn", model="claude-opus-5-5", i=1500, o=321):
    return {"id": "msg_stub", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": i, "output_tokens": o}}


class VisionCallViaStubServer(unittest.TestCase):
    ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "NO_PROXY", "no_proxy", "MULTIMODAL_MODEL",
           "MULTIMODAL_ALLOW_MANUAL")

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.ENV}
        os.environ["ANTHROPIC_API_KEY"] = "stub-key-for-tests"
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        os.environ.pop("MULTIMODAL_MODEL", None)
        self.tmp = tempfile.mkdtemp()
        _o, _s, skipped = load_evidence(DATASET)
        self.ev = next(e for e in skipped if e.file_id == "evidence_28")      # support ticket PNG
        events, _a, _l = parse_activity(DATASET)
        self.aliases = resource_aliases(events)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def run_once(self, api, **kw):
        os.environ["ANTHROPIC_BASE_URL"] = api.url
        log = io.StringIO()
        out = multimodal_observations([self.ev], self.aliases, cache_dir=Path(self.tmp), log=log,
                                      manual_dir=Path(self.tmp) / "none", frozen_dir=Path(self.tmp) / "none2", **kw)
        return out, log.getvalue()

    GOOD = json.dumps({"records": [{
        "resource_id": "support_1034", "keys": ["CS-5034"], "system_id": "support_platform",
        "category": "SUPPORT_MESSAGE", "state": "IDENTIFIABLE", "identifiers": ["Kagyeman8947@fastmail.com"],
        "observed_at": "", "notes": "stub"}]})

    def test_real_sdk_request_shape_cache_and_usage(self):
        api = _StubAPI(200, _message(self.GOOD))
        try:
            out, log = self.run_once(api)
            self.assertEqual(len(api.requests), 1)
            req = api.requests[0]
            self.assertEqual(req["path"], "/v1/messages")
            auth = req["headers"].get("x-api-key") or req["headers"].get("Authorization") or ""
            self.assertIn("stub-key-for-tests", auth)             # the SDK authenticates with the env key
            body = req["json"]
            self.assertEqual(body["model"], "claude-opus-5-5")
            block = body["messages"][0]["content"][0]
            self.assertEqual((block["type"], block["source"]["type"], block["source"]["media_type"]),
                             ("image", "base64", "image/png"))
            self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
            self.assertEqual(body["output_config"]["effort"], "low")
            self.assertFalse(body["output_config"]["format"]["schema"]["additionalProperties"])
            o, = out
            self.assertEqual((o.resource_id, o.state, o.source_kind), ("support_1034", "IDENTIFIABLE", "image"))
            self.assertEqual(o.identifiers, {"kagyeman8947@fastmail.com"})
            # real usage from the response is what gets recorded
            usage = json.loads((Path(self.tmp) / "usage.json").read_text())
            self.assertEqual((usage["api_calls_count"], usage["input_tokens_this_run"], usage["output_tokens_this_run"]),
                             (1, 1500, 321))
            self.assertEqual(usage["api_calls_this_run"][0]["served_model"], "claude-opus-5-5")
            # second run: served from the cache, zero calls, same observations, cached usage kept
            out2, _ = self.run_once(api)
            self.assertEqual(len(api.requests), 1)
            self.assertEqual([x.resource_id for x in out2], ["support_1034"])
            usage2 = json.loads((Path(self.tmp) / "usage.json").read_text())
            self.assertEqual(usage2["api_calls_count"], 0)
            self.assertEqual(usage2["cached_usage_total"]["input_tokens"], 1500)
            self.assertEqual(usage2["cached_usage_total"]["output_tokens"], 321)
        finally:
            api.close()

    def test_api_error_never_raises_and_nothing_is_cached(self):
        api = _StubAPI(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})
        try:
            out, log = self.run_once(api)
            self.assertEqual(out, [])
            self.assertIn("API call failed", log)
            self.assertNotIn("stub-key-for-tests", log)
            self.assertEqual(list(Path(self.tmp).glob("*.json")), [Path(self.tmp) / "usage.json"])
        finally:
            api.close()

    def test_refusal_and_malformed_json_are_skipped_but_tokens_counted(self):
        for msg in (_message("", stop="refusal"), _message("not json at all")):
            api = _StubAPI(200, msg)
            try:
                out, log = self.run_once(api)
                self.assertEqual(out, [])
                self.assertIn("not cached", log)
                usage = json.loads((Path(self.tmp) / "usage.json").read_text())
                self.assertEqual(usage["api_calls_count"], 1)     # the call happened and cost tokens
            finally:
                api.close()

    def test_model_comes_from_environment_and_effort_only_for_known_families(self):
        os.environ["MULTIMODAL_MODEL"] = "claude-sonnet-4-6"
        api = _StubAPI(200, _message(self.GOOD, model="claude-sonnet-4-6"))
        try:
            self.run_once(api)
            body = api.requests[0]["json"]
            self.assertEqual(body["model"], "claude-sonnet-4-6")
            self.assertNotIn("effort", body["output_config"])
        finally:
            api.close()


class DotenvAndLookupOrder(unittest.TestCase):
    def test_dotenv_loads_only_whitelisted_names_and_never_overrides(self):
        saved = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "MULTIMODAL_MODEL", "SOME_OTHER_VAR")}
        for k in saved:
            os.environ.pop(k, None)
        try:
            with tempfile.TemporaryDirectory() as d:
                env = Path(d) / ".env"
                env.write_text('# comment\nexport ANTHROPIC_API_KEY="sk-from-dotenv"\n'
                               "MULTIMODAL_MODEL=claude-sonnet-5-5\nSOME_OTHER_VAR=leak\n\nbroken line\n")
                os.environ["MULTIMODAL_MODEL"] = "already-set"
                names = multimodal.load_dotenv([env])
            self.assertEqual(names, ["ANTHROPIC_API_KEY"])
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-from-dotenv")
            self.assertEqual(os.environ["MULTIMODAL_MODEL"], "already-set")
            self.assertNotIn("SOME_OTHER_VAR", os.environ)
            self.assertEqual(multimodal.load_dotenv([Path("/nonexistent/.env")]), [])
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_lookup_order_api_cache_beats_frozen_beats_manual(self):
        ev = next(e for e in load_evidence(DATASET)[2] if e.file_id == "evidence_28")
        def entry(method, state):
            return {"file_id": ev.file_id, "file_sha256": multimodal._sha256(ev.path),
                    "extractor": {"method": method, "model": multimodal._model(), "prompt_version": multimodal.PROMPT_VERSION},
                    "usage": None, "records": [{"resource_id": "support_1034", "keys": [], "system_id": "support_platform",
                                                "category": "SUPPORT_MESSAGE", "state": state, "identifiers": [],
                                                "observed_at": "", "notes": ""}]}
        with tempfile.TemporaryDirectory() as c, tempfile.TemporaryDirectory() as f, tempfile.TemporaryDirectory() as m:
            c, f, m = Path(c), Path(f), Path(m)
            key = multimodal._cache_key(ev.path, multimodal._model())
            (m / f"{multimodal._sha256(ev.path)[:16]}__manual.json").write_text(json.dumps(entry("manual_transcription", "FOUND")))
            self.assertEqual(multimodal._cached_entry(ev, c, m, f)["records"][0]["state"], "FOUND")      # manual fallback
            (f / f"{key}.json").write_text(json.dumps(entry("api", "REDACTED")))
            self.assertEqual(multimodal._cached_entry(ev, c, m, f)["records"][0]["state"], "REDACTED")   # frozen beats manual
            (c / f"{key}.json").write_text(json.dumps(entry("api", "IDENTIFIABLE")))
            self.assertEqual(multimodal._cached_entry(ev, c, m, f)["records"][0]["state"], "IDENTIFIABLE")  # cache beats frozen
            os.environ["MULTIMODAL_ALLOW_MANUAL"] = "0"
            try:
                (c / f"{key}.json").unlink(); (f / f"{key}.json").unlink()
                self.assertIsNone(multimodal._cached_entry(ev, c, m, f))           # strict final run: no manual
            finally:
                del os.environ["MULTIMODAL_ALLOW_MANUAL"]


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

    def test_report_and_freeze_use_only_api_results(self):
        mm, tool = multimodal, self.tool
        ev = next(e for e in load_evidence(DATASET)[2] if e.file_id == "evidence_28")
        with tempfile.TemporaryDirectory() as d:
            cache, frozen = Path(d) / "cache", Path(d) / "frozen"
            cache.mkdir()
            manual = tool._manual_entry(ev, mm.MANUAL_DIR)
            api = {"file_id": ev.file_id, "file_sha256": mm._sha256(ev.path),
                   "extractor": {"method": "api", "model": mm._model(), "prompt_version": mm.PROMPT_VERSION},
                   "usage": {"input_tokens": 1, "output_tokens": 1}, "records": manual["records"]}   # synthetic: copy of the manual one
            (cache / f"{mm._cache_key(ev.path, mm._model())}.json").write_text(json.dumps(api))
            text, tot = tool.build_report(DATASET, cache, frozen, mm.MANUAL_DIR)
            self.assertEqual((tot["compared"], tot["files"], tot["identical"]), (1, 11, 1))
            self.assertIn("no VLM result", text)
            self.assertEqual(tool.freeze(DATASET, cache, frozen), 1)
            self.assertEqual(len(list(frozen.glob("*.json"))), 1)
            # a manual transcription is never reported as a VLM result
            self.assertIsNone(tool._vlm_entry(ev, Path(d) / "none", Path(d) / "none2"))


if __name__ == "__main__":
    unittest.main()
