"""Request-window linking through the pipeline's call pattern (Person B).

Same person, two requests months apart; a resource that appears only in the first request's
window must be linked to the first case and NOT to the second. Skipped automatically when
state.py has no RequestRef / EvidenceIndex.link_requests (older Person A snapshots).
Run from repo root:  python3 -m unittest discover -s code/tests -v
"""
import sys
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import state  # noqa: E402
from models import Identity, Observation  # noqa: E402

HAS_RULE = hasattr(state, "RequestRef") and hasattr(state.EvidenceIndex, "link_requests")


def ob(rid, system, cat, st, t, ev, ids):
    return Observation(rid, system, cat, st, set(ids), t, ev, "test", raw={}, captured_at=t)


@unittest.skipUnless(HAS_RULE, "state.py has no RequestRef/link_requests (merge Person A's request-window rule)")
class RequestWindowLinking(unittest.TestCase):
    def test_resource_of_first_window_is_not_linked_to_later_request(self):
        email = "same.person@example.com"
        ident = Identity("user_900", emails={email})
        idx = state.EvidenceIndex([
            ob("export_9001", "export_service", "USER_EXPORT", "FOUND",
               "2026-03-10T09:00:00Z", "evidence_t1", [email]),
        ], scopes=[])
        first = state.RequestRef("case_a", ident, "2026-03-20T00:00:00Z", "2026-03-01T00:00:00Z")
        second = state.RequestRef("case_b", ident, "2026-09-20T00:00:00Z", "2026-09-01T00:00:00Z")
        linked = idx.link_requests([first, second])
        self.assertIn("export_9001", linked.get("case_a", {}))
        self.assertNotIn("export_9001", linked.get("case_b", {}))


if __name__ == "__main__":
    unittest.main()
