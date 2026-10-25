"""usage_report.py on synthetic usage files (Person B): one row per provider+model, both usage
files merged, free tier, unset prices. Run: python3 -m unittest discover -s code/tests -v"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / "evaluation" / "usage_report.py"


def load():
    spec = importlib.util.spec_from_file_location("usage_report_under_test", PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PRICES = {"unit": "USD per MTok", "gemini_tier": "paid", "models": {
    "gemini/gemini-3.5-flash-lite": {"input": 0.25, "output": 1.50, "source": "u", "retrieved": "2026-01-01"},
    "gemini/gemini-3.6-flash": {"input": None, "output": None, "source": "u", "retrieved": None},
    "anthropic/claude-haiku-5-5": {"input": 0.10, "output": 0.50, "source": "u", "retrieved": "2026-01-01"}}}


class UsageReport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".cache").mkdir()
        (self.tmp / "evaluation").mkdir()

    def write(self, name, data):
        (self.tmp / name).write_text(json.dumps(data))

    def collect(self, prices=PRICES):
        self.write("evaluation/prices.json", prices)
        m = load()
        with mock.patch.object(m, "CODE", self.tmp), mock.patch.object(m, "CACHE", self.tmp / ".cache"), \
                mock.patch.object(m, "MULTIMODAL_USAGE_CANDIDATES", [self.tmp / ".cache" / "multimodal_usage.json"]):
            return m, m.collect()

    def test_merges_both_files_into_one_row_per_provider_and_model(self):
        self.write(".cache/classifier_usage.json", [
            {"provider": "gemini", "model": "gemini-3.5-flash-lite", "input_tokens": 1000, "output_tokens": 400, "ok": True},
            {"provider": "gemini", "model": "gemini-3.5-flash-lite", "input_tokens": 500, "output_tokens": 100, "ok": False}])
        self.write(".cache/multimodal_usage.json", [
            {"provider": "gemini", "model": "gemini-3.5-flash-lite", "input_tokens": 200, "output_tokens": 50},
            {"model": "claude-haiku-5-5", "input_tokens": 70, "output_tokens": 30}])         # provider inferred from the name
        _, d = self.collect()
        rows = {(r["provider"], r["model"]): r for r in d["rows"]}
        self.assertEqual(len(rows), 2)
        g = rows[("gemini", "gemini-3.5-flash-lite")]
        self.assertEqual((g["calls"], g["failed"], g["input"], g["output"]), (3, 1, 1700, 550))
        self.assertEqual(rows[("anthropic", "claude-haiku-5-5")]["calls"], 1)
        self.assertAlmostEqual(g["cost"], (1700 * 0.25 + 550 * 1.50) / 1e6)
        self.assertEqual((d["calls"], d["input"], d["output"]), (4, 1770, 580))
        self.assertTrue(d["cost_ok"])

    def test_unset_price_is_reported_not_guessed(self):
        self.write(".cache/classifier_usage.json", [
            {"provider": "gemini", "model": "gemini-3.6-flash", "input_tokens": 10, "output_tokens": 5}])
        m, d = self.collect()
        self.assertIsNone(d["rows"][0]["cost"])
        self.assertFalse(d["cost_ok"])
        text = m.render(d)
        self.assertIn("price not set", text)
        self.assertIn("incomplete", text)

    def test_free_tier_shows_zero_with_the_paid_price_as_reference(self):
        self.write(".cache/classifier_usage.json", [
            {"provider": "gemini", "model": "gemini-3.5-flash-lite", "input_tokens": 1_000_000, "output_tokens": 0}])
        m, d = self.collect({**PRICES, "gemini_tier": "free"})
        r = d["rows"][0]
        self.assertEqual((r["cost"], r["free_tier"]), (0.0, True))
        self.assertAlmostEqual(r["paid_reference"], 0.25)
        self.assertIn("free tier", m.render(d))
        self.assertEqual(d["cost_total"], 0.0)

    def test_dated_model_names_match_their_price_entry_and_missing_files_are_noted(self):
        self.write(".cache/classifier_usage.json", [
            {"provider": "gemini", "model": "gemini-3.5-flash-lite-001", "input_tokens": 4, "output_tokens": 2,
             "usage_reported": False}])
        m, d = self.collect()
        self.assertTrue(d["cost_ok"])                         # prefix match found the price entry
        text = m.render(d)
        self.assertIn("no usage metadata", text)
        self.assertIn("multimodal_usage.json", text)          # absence of Person A's file is stated
        self.assertEqual(d["by_component"]["Request classifier"]["calls"], 1)

    def test_no_files_means_zero_not_an_estimate(self):
        m, d = self.collect()
        self.assertEqual((d["calls"], d["input"], d["output"], d["rows"]), (0, 0, 0, []))
        self.assertIn("0 calls, 0 tokens", m.render(d))


if __name__ == "__main__":
    unittest.main()
