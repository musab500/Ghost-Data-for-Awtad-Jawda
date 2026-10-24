"""Tests for the evidence parsers (Person A). Run from repo root:
    python3 -m unittest discover -s code/tests -v
"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))
DATASET = CODE.parent / "dataset"

from models import (Identity, Observation, normalize_identifier, norm_phone,  # noqa: E402
                    parse_ts)
from parsers import load_evidence  # noqa: E402
from parsers.activity import parse_activity  # noqa: E402
from parsers.logs import parse_line  # noqa: E402
from parsers.manifest import EvidenceFile  # noqa: E402
from parsers.sql import parse_sql  # noqa: E402


class ParsedCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.obs, cls.scopes, cls.skipped = load_evidence(DATASET)

    def by_rid(self, rid):
        return [o for o in self.obs if o.resource_id == rid]

    def test_only_media_skipped(self):
        self.assertEqual({e.kind for e in self.skipped}, {"png", "pdf"})

    def test_case_11_resources_link_to_user_32(self):
        idt = Identity("user_32", emails={"novak.tomasz@icloud.com"})
        toks = idt.tokens()
        cutoff = parse_ts("2026-10-01T12:00:00Z")
        expected = {
            ("account_service", "account_4248"): "NOT_FOUND",
            ("search_service", "search_052"): "FOUND",
            ("analytics_platform", "analytics_2034"): "IDENTIFIABLE",
            ("support_platform", "support_864"): "IDENTIFIABLE",
            ("integration_service", "data_flow_9522"): "ACTIVE",
            ("security_service", "audit_record_9394"): "FOUND",
        }
        for (system, rid), state in expected.items():
            hits = [o for o in self.by_rid(rid) if o.identifiers & toks
                    and parse_ts(o.captured_at) <= cutoff]
            self.assertTrue(hits, rid)
            self.assertEqual({(o.system_id, o.state, o.evidence_id) for o in hits},
                             {(system, state, "evidence_03")}, rid)

    def test_empty_snapshot_scope_proves_absence(self):
        # evidence_51 holds an empty customer_profiles snapshot (case_31).
        sc = [s for s in self.scopes if s.evidence_id == "evidence_51"
              and s.system_id == "account_service"]
        self.assertTrue(sc)
        self.assertNotIn("account_4857", set().union(*(s.present for s in sc)))

    def test_replica_drift_separate_copies(self):
        # account_005 is in the failover replica but not the primary (case_05).
        copies = {s.copy: s.present for s in self.scopes if s.evidence_id == "evidence_54"}
        self.assertIn("account_005", copies["customer_profiles_failover_replica"])
        self.assertNotIn("account_005", copies["customer_profiles_primary"])

    def test_log_trace_links_lookup_key_to_result(self):
        # resource_lookup_trace: started line has lookup_key, completed line has result.
        o = [x for x in self.by_rid("account_6523") if x.evidence_id == "evidence_67"]
        self.assertEqual(o[0].state, "NOT_FOUND")
        self.assertIn("leonie.g9352@outlook.com", o[0].identifiers)

    def test_marketing_contact_linked_by_email_hash(self):
        # Linked through the hash of user_12's *historical* email (user_audit_log).
        h = hashlib.sha256(b"minjun.kim90@gmail.com").hexdigest()
        o = [x for x in self.by_rid("analytics_9914") if x.evidence_id == "evidence_01"]
        self.assertEqual((o[0].category, o[0].state), ("MARKETING_PROFILE", "FOUND"))
        self.assertIn(h, o[0].identifiers)

    def test_legacy_contact_index_is_secondary_identifier(self):
        o = [x for x in self.by_rid("support_002") if x.evidence_id == "evidence_61"]
        self.assertEqual(o[0].category, "SECONDARY_IDENTIFIER")
        self.assertIn("nadia.ionescu@yahoo.com", o[0].identifiers)

    def test_tombstone_with_direct_identifiers_is_identifiable(self):
        states = {o.state for o in self.by_rid("tombstone_9178") if o.evidence_id == "evidence_51"}
        self.assertEqual(states, {"IDENTIFIABLE"})
        states = {o.state for o in self.by_rid("tombstone_440") if o.evidence_id == "evidence_51"}
        self.assertEqual(states, {"REDACTED"})

    def test_every_observation_has_manifest_capture_time(self):
        for o in self.obs:
            self.assertTrue(o.captured_at, o)
            parse_ts(o.observed_at)


class Units(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(normalize_identifier("CHOI.JIHOON94@GMAIL.COM"), "choi.jihoon94@gmail.com")
        self.assertEqual(norm_phone("+48 601 623 422"), "+48601623422")
        self.assertEqual(normalize_identifier("User_32"), "user_32")
        self.assertIsNone(normalize_identifier("null"))

    def test_all_log_layouts(self):
        lines = [
            '2026-07-03T07:51:32Z | DEBUG | security_service | tr-1 | audit_record_read_completed | resource_id="audit_record_1" rows="1"',
            '2026-07-28T05:51:21Z [INFO] [account_service] [tr-2] profile_lookup_completed: resource_id="account_1" rows="0" http_status="404"',
            '2026-03-25T08:51:23Z <12>1 node-1 support_platform tr-3 - - level=WARN resource_id="support_1" event="message_pii_scan" matched_value="a@b.co"',
            '2026-04-19T06:51:41Z {"service":"backup_service","trace_id":"tr-4","resource_id":"backup_1","event":"restore_candidate_evaluated","eligible":"true","deletion_block":"false"}',
            '2026-09-30T11:51:03Z requestId=tr-5 level=DEBUG function=profile_service message="resource_id=\\"account_1\\" event=\\"profile_lookup_completed\\" rows=\\"1\\""',
        ]
        for line in lines:
            p = parse_line(line)
            self.assertIsNotNone(p["service"], line)
            self.assertIsNotNone(p["event"], line)
            self.assertIn("resource_id", p["fields"], line)

    def test_temporal_fields_from_sql_header(self):
        # Synthetic snapshot: observation time comes from the header, not row time.
        sql = ("-- Captured at: 2026-05-01T00:00:00Z\nBEGIN TRANSACTION;\n"
               "CREATE TABLE customer_data_primary (resource_id TEXT, origin_system TEXT, "
               "collection_name TEXT, record_json TEXT, updated_at TEXT);\n"
               "INSERT INTO customer_data_primary VALUES('data_flow_1','integration_service',"
               "'connector_configurations','{\"resource_id\":\"data_flow_1\",\"enabled\":false,"
               "\"subject_selector\":\"x@y.com\"}','2026-04-01T00:00:00Z');\nCOMMIT;\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snap.sql"
            p.write_text(sql)
            obs, scopes = parse_sql(EvidenceFile("evidence_x", p, "snap.sql", "t", "2026-05-02T00:00:00Z"))
        self.assertEqual((obs[0].state, obs[0].observed_at), ("DISABLED", "2026-05-01T00:00:00Z"))
        self.assertEqual(obs[0].captured_at, "2026-05-02T00:00:00Z")
        self.assertEqual(scopes[0].present, {"data_flow_1"})

    def test_activity_claims_vs_evidence(self):
        events, obs, links = parse_activity(DATASET)
        self.assertEqual(len(events), 4011)
        link = [l for l in links if l.resource_id == "object_19"][0]
        self.assertTrue(link.exclusive)
        self.assertIn("priya.raman@gmail.com", link.identifiers)
        # A SUCCESS deletion claim never becomes an Observation.
        self.assertFalse([o for o in obs if o.resource_id == "account_001"])
        self.assertTrue(all(o.state in ("ACTIVE", "DISABLED", "BLOCKED") for o in obs))

    def test_invalid_state_rejected(self):
        with self.assertRaises(ValueError):
            Observation("r", "s", None, "MAYBE", set(), "2026-01-01T00:00:00Z", "e", "sql")


if __name__ == "__main__":
    unittest.main()
