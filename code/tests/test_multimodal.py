"""Tests for multimodal.py (Person A). No network: the vision API is never
called because ANTHROPIC_API_KEY is removed for these tests."""
import io
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
            out = multimodal_observations(png, self.aliases, cache_dir=Path(d), log=log)
            self.assertEqual(out, [])
            self.assertIn("not processed", log.getvalue())
            usage = (Path(d) / "usage.json").read_text()
            self.assertIn('"api_calls_count": 0', usage)

    def test_manual_entries_can_be_disabled(self):
        os.environ["MULTIMODAL_ALLOW_MANUAL"] = "0"
        try:
            with tempfile.TemporaryDirectory() as d:
                for p in multimodal.CACHE_DIR.glob("*__manual.json"):
                    shutil.copy(p, d)
                png = [e for e in self.skipped if e.kind == "png"]
                out = multimodal_observations(png, self.aliases, cache_dir=Path(d), log=io.StringIO())
                self.assertEqual(out, [])
        finally:
            del os.environ["MULTIMODAL_ALLOW_MANUAL"]


if __name__ == "__main__":
    unittest.main()
