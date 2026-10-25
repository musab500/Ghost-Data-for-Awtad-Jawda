"""Evidence index: which resources belong to a person, and what state each
resource was in at a case's audit_at.

Built once from the parser outputs and reused for every case.

Linking (resource -> person)
  1. Direct: an Observation of the resource carries one of the person's
     identifier tokens (current/historical email, phone, email hash, user_id).
  2. Catalog: a LINK_RESOURCE_IDENTITY activity names the person.
  3. Workflow fallback: the deletion workflow acted on the resource close to
     the case's reported_at (the workflow run that produced the claim), *and*
     no evidence links the resource to any person. Used only for resources that carry no identifier at all
     (deleted accounts, anonymised analytics rows).

State at audit_at
  Only evidence whose manifest capture time is <= audit_at is used. The most
  recent explicit observation wins; when several copies (primary / replica
  tables) were observed at that same latest time, the record counts as
  present if *any* copy still holds it. Only if no explicit observation
  exists, a snapshot (Scope) of the system that omits a deletable resource
  proves it absent.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from models import (ACTIVE, FOUND, IDENTIFIABLE, NOT_FOUND, ActivityEvent,
                    Identity, Observation, ResourceLink, Scope,
                    category_for_resource, iso, parse_ts)

PRESENT_STATES = {FOUND, IDENTIFIABLE, ACTIVE}
# Categories whose absence may be inferred from a snapshot that omits them.
SCOPE_ABSENCE_CATEGORIES = {"USER_PROFILE", "SEARCH_INDEX_ENTRY", "USER_EXPORT",
                            "SUPPORT_ATTACHMENT", "SECONDARY_IDENTIFIER",
                            "CACHE_ENTRY", "MARKETING_PROFILE"}
# The workflow's primary-profile deletion event is what the reported status
# summarises, so its reported_result normally agrees with the case's claim.
# Used only as a tie-breaker when joining activity to a case, never as proof.
STATUS_TO_ACCOUNT_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL",
                            "NOT_STARTED": "FAILED"}
STATUS_MISMATCH_PENALTY = 45.0
# Extra cost for a case expected to be COMPLETE, by the candidate's evidence
# tier at audit_at (0 explicit observation, 1 snapshot omission only, 2 none).
CITATION_TIER_PENALTY = (0.0, 5.0, 20.0)
# Workflow-run shape. A run that finishes before the report performs one
# policy action per time slot (analytics/tombstone, support, data flow,
# backup); a run that fails retries after the report.
SLOT_TOLERANCE_MIN = 2.0       # actions this close share a slot
RUN_AFFINITY_BONUS = 10.0      # case owns sibling actions of the same run
FAILED_RUN_PENALTY = 15.0      # case's own actions are post-report retries only
# Where a case's own workflow actions sit relative to its reported_at
# (minutes): the primary-profile deletion is submitted first, policy actions
# on identifier-less copies (anonymise, redact, disable, block) follow just
# before the report. (lo, hi, expected)
ACCOUNT_WINDOW = (-90.0, 10.0, -60.0)
POLICY_WINDOW = (-35.0, 10.0, -20.0)
# Workflow actions whose target resource may belong to the request subject.
WORKFLOW_SOURCES = {"deletion_worker"}
WORKFLOW_TYPES = {"DELETE_RECORD", "ANONYMIZE_RECORD", "REDACT_RECORD",
                  "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "RETAIN_RECORD",
                  "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS"}


@dataclass
class CaseWindow:
    """What workflow assignment needs to know about one request."""
    case_id: str
    reported_at: str
    audit_at: str
    direct_categories: set[str | None] = field(default_factory=set)
    reported_status: str = ""
    # True when every directly linked resource is already compliant, i.e. the
    # case is expected to be COMPLETE and its verification steps must cite
    # evidence. Such cases prefer candidates with stronger citable evidence.
    expects_complete: bool = False
    # Resources directly linked to the case; their workflow actions reveal the
    # shape of the case's workflow run (see assign_workflow).
    direct_resources: set[str] = field(default_factory=set)


@dataclass
class RequestRef:
    """One deletion request, as far as record linking is concerned."""
    case_id: str
    identity: Identity | None      # resolved identity (None = email resolved to nobody)
    audit_at: str
    requested_at: str = ""


def group_by_person(refs: list[RequestRef]) -> dict[str, list[RequestRef]]:
    """Requests grouped by resolved Identity.user_id (never by email: a person
    may use an old and a new address), each group ordered by
    (audit_at, requested_at, case_id)."""
    groups: dict[str, list[RequestRef]] = defaultdict(list)
    for r in refs:
        if r.identity is not None:
            groups[r.identity.user_id.lower()].append(r)
    for rs in groups.values():
        rs.sort(key=lambda r: (parse_ts(r.audit_at), r.requested_at, r.case_id))
    return dict(groups)


def request_windows(refs: list[RequestRef]) -> dict[str, tuple[str | None, bool]]:
    """{case_id: (not_before, has_later_request)} - the evidence window of each
    request of a person.

    Let a_1 < ... < a_n be the DISTINCT audit_at times of one person's requests.
    Request k owns linking evidence captured in
        W_1 = (-inf, a_1]      W_k = (a_(k-1), a_k]      W_n = (a_(n-1), +inf)
    * Upper bounds are inclusive ("at or before audit_at"); lower bounds are
      exclusive, so evidence captured exactly at a_(k-1) belongs to request k-1.
    * The last request is open-ended upward: evidence captured after its audit
      still identifies the person's records (identity is not state), while
      state_at() never uses it.
    * Requests of one person with the same audit_at share one window.
    * A person with a single request has the window (-inf, +inf)."""
    out: dict[str, tuple[str | None, bool]] = {}
    for rs in group_by_person(refs).values():
        times = sorted({parse_ts(r.audit_at) for r in rs})
        for r in rs:
            t = parse_ts(r.audit_at)
            earlier = [x for x in times if x < t]
            prev = max(earlier) if earlier else None
            out[r.case_id] = (iso(prev) if prev else None, any(x > t for x in times))
    return out


@dataclass
class ResourceState:
    system_id: str
    resource_id: str
    category: str | None
    state: str | None              # None = no usable evidence at audit_at
    evidence_id: str = ""          # evidence proving `state`
    observed_at: str = ""
    link_reasons: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class EvidenceIndex:
    def __init__(self, observations: list[Observation], scopes: list[Scope],
                 links: list[ResourceLink] = (), events: list[ActivityEvent] = ()):
        self.obs_by_rid: dict[str, list[Observation]] = defaultdict(list)
        self.rids_by_token: dict[str, set[str]] = defaultdict(set)
        for o in observations:
            self.obs_by_rid[o.resource_id].append(o)
            for t in o.identifiers:
                self.rids_by_token[t].add(o.resource_id)
        self.links_by_rid: dict[str, list[ResourceLink]] = defaultdict(list)
        for l in links:
            self.links_by_rid[l.resource_id].append(l)
            for t in l.identifiers:
                self.rids_by_token[t].add(l.resource_id)
        self.scopes_by_system: dict[str, list[Scope]] = defaultdict(list)
        for s in scopes:
            self.scopes_by_system[s.system_id].append(s)
        self.events = sorted(events, key=lambda e: parse_ts(e.event_time))
        self.system_of: dict[str, str] = {}
        for o in observations:
            self.system_of.setdefault(o.resource_id, o.system_id)
        for e in self.events:
            if e.source_system in WORKFLOW_SOURCES:
                self.system_of.setdefault(e.resource_id, e.target_system)

    # ------------------------------------------------------------- linking
    def tokens_of_rid(self, rid: str) -> set[str]:
        toks = set()
        for o in self.obs_by_rid.get(rid, ()):
            toks |= o.identifiers
        for l in self.links_by_rid.get(rid, ()):
            toks |= l.identifiers
        return toks

    def direct_links(self, identity: Identity, not_before: str | None = None,
                     audit_at: str | None = None,
                     has_later_request: bool = False) -> dict[str, list[str]]:
        """{resource_id: [reasons]} for resources carrying the person's identifiers.

        Request windows (see request_windows): a resource belongs to the ONE
        request whose window contains its FIRST linking evidence (the earliest
        capture time of an observation, or catalog link, carrying one of the
        person's identifiers). So, for this request:
        * first linking evidence at or before not_before (the previous request's
          audit_at) -> it belongs to an earlier request, and is not repeated;
        * first linking evidence after audit_at while the person has a later
          request (has_later_request) -> it belongs to that later request;
        * otherwise it belongs here. For the person's last request this includes
          evidence captured after audit_at (identity is not state); the state at
          audit_at is still only ever taken from evidence captured by then.
        A person with a single request has no window: nothing is excluded.

        A person has one primary account. When several USER_PROFILE records
        are linked, accounts with linking evidence inside the request window
        win over accounts only seen later, and among those the most recently
        linked is kept."""
        toks = identity.tokens()
        nb = parse_ts(not_before) if not_before else None
        cut = parse_ts(audit_at) if audit_at else None
        out: dict[str, list[str]] = {}
        rank: dict[str, tuple] = {}
        rids = sorted({r for t in toks for r in self.rids_by_token.get(t, ())})
        for rid in rids:
            times = [parse_ts(o.captured_at) for o in self.obs_by_rid.get(rid, ()) if o.identifiers & toks]
            times += [parse_ts(l.observed_at) for l in self.links_by_rid.get(rid, ()) if l.identifiers & toks]
            if not times:
                continue
            first = min(times)
            if nb is not None and first <= nb:
                continue                    # owned by an earlier request
            if has_later_request and cut is not None and first > cut:
                continue                    # owned by a later request
            in_window = [t for t in times if (nb is None or t > nb) and (cut is None or t <= cut)]
            rank[rid] = (bool(in_window), max(in_window or times))
            out[rid] = [f"identifier:{t}" for t in sorted(toks & self.tokens_of_rid(rid))]
        accounts = [r for r in out if self.category_of(r) == "USER_PROFILE"]
        if len(accounts) > 1:
            best = max(rank[r] for r in accounts)
            for r in accounts:
                if rank[r] < best:
                    del out[r]
        return out

    def link_requests(self, refs: list[RequestRef]) -> dict[str, dict[str, list[str]]]:
        """{case_id: {resource_id: reasons}} for ALL requests at once: groups
        them by resolved user_id, derives each person's evidence windows
        (request_windows) and calls direct_links with them. Pass the samples and
        the evaluation requests together: a person's earlier request may be in
        either file. A request with no identity gets {}."""
        windows = request_windows(refs)
        out: dict[str, dict[str, list[str]]] = {}
        for r in refs:
            if r.identity is None:
                out[r.case_id] = {}
                continue
            nb, later = windows[r.case_id]
            out[r.case_id] = self.direct_links(r.identity, nb, r.audit_at, later)
        return out

    def category_of(self, rid: str) -> str | None:
        return _best_category(self.obs_by_rid.get(rid, ()), rid)

    def assign_workflow(self, cases: list[CaseWindow]) -> dict[str, dict[str, str]]:
        """Assign identifier-less resources touched by the deletion workflow to
        cases, globally across *all* requests (the activity stream is shared).

        A workflow event is a candidate for a case when it falls in the
        expected slot of the case's workflow run (ACCOUNT_WINDOW for the
        primary-profile deletion, POLICY_WINDOW for other actions) and not
        after audit_at. Each resource goes to at most one case, and each case
        receives at most one resource per category, none for a category it
        already has a directly linked resource for. The assignment is a
        minimum-cost bipartite matching (per category). Candidate cost is
        distance from the expected slot, plus a penalty when an account
        deletion's reported_result disagrees with the case's reported_status,
        plus workflow-run shape terms for non-account actions: a case whose
        own resources already fill the candidate's time slot cannot take it,
        a case owning sibling actions of the same pre-report run gets a bonus,
        and a case whose own actions are only post-report retries a penalty;
        plus, for a case expected to be COMPLETE, a cost for candidates whose
        state at audit_at is only weakly citable (see evidence_tier): such a
        case must cite proof for every verification step, while an INCOMPLETE
        case's verification steps are future checks with no citation.

        Returns {case_id: {resource_id: reason}}."""
        cand = []
        for c in cases:
            rep = parse_ts(c.reported_at)
            cut = parse_ts(c.audit_at)
            # Offsets (minutes) of workflow actions on the case's own resources.
            own = [(parse_ts(e.event_time) - rep).total_seconds() / 60
                   for e in self.events
                   if e.resource_id in c.direct_resources and e.source_system in WORKFLOW_SOURCES
                   and not (e.event_type == "DELETE_RECORD" and e.target_system == "account_service")]
            own_pre = [o for o in own if POLICY_WINDOW[0] <= o <= 0]
            own_post = [o for o in own if o > POLICY_WINDOW[1]]
            for e in self.events:
                if e.source_system not in WORKFLOW_SOURCES or e.event_type not in WORKFLOW_TYPES:
                    continue
                t = parse_ts(e.event_time)
                if t > cut:
                    continue
                offset = (t - rep).total_seconds() / 60
                is_account = e.event_type == "DELETE_RECORD" and e.target_system == "account_service"
                lo, hi, expected = ACCOUNT_WINDOW if is_account else POLICY_WINDOW
                if not lo <= offset <= hi:
                    continue
                if self.tokens_of_rid(e.resource_id):
                    continue          # carries identifiers: linked directly or not at all
                cat = self.category_of(e.resource_id)
                if cat in c.direct_categories:
                    continue
                score = abs(offset - expected)
                if (is_account and c.reported_status in STATUS_TO_ACCOUNT_RESULT
                        and e.reported_result != STATUS_TO_ACCOUNT_RESULT[c.reported_status]):
                    score += STATUS_MISMATCH_PENALTY
                if c.expects_complete:
                    score += CITATION_TIER_PENALTY[self.evidence_tier(e.resource_id, c.audit_at)]
                if not is_account:
                    if any(abs(o - offset) <= SLOT_TOLERANCE_MIN for o in own):
                        continue      # this slot of the case's run is already filled
                    if own_pre:
                        score -= RUN_AFFINITY_BONUS
                    elif own_post:
                        score += FAILED_RUN_PENALTY
                cand.append((score, c.case_id, e.resource_id, cat, e.activity_id))
        return _min_cost_assignment(cand)

    def evidence_tier(self, rid: str, audit_at: str) -> int:
        """How the resource's state at audit_at can be cited:
        0 = explicit observation, 1 = only a snapshot that omits it, 2 = none."""
        rs = self.state_at(rid, audit_at)
        if rs.state is None:
            return 2
        return 1 if "scope" in rs.raw else 0

    # --------------------------------------------------------------- state
    def state_at(self, rid: str, audit_at: str) -> ResourceState:
        cut = parse_ts(audit_at)
        usable = [o for o in self.obs_by_rid.get(rid, ())
                  if parse_ts(o.captured_at) <= cut and parse_ts(o.observed_at) <= cut]
        system = self.system_of.get(rid) or (usable[0].system_id if usable else "")
        category = _best_category(self.obs_by_rid.get(rid, ()), rid)
        if usable:
            latest_t = max(parse_ts(o.observed_at) for o in usable)
            latest = [o for o in usable if parse_ts(o.observed_at) == latest_t]
            present = [o for o in latest if o.state in PRESENT_STATES]
            pick = sorted(present or latest, key=_evidence_sort_key)[0]
            return ResourceState(pick.system_id or system, rid, category, pick.state,
                                 pick.evidence_id, pick.observed_at, raw=pick.raw)
        # Snapshot listings can be partial, so omission is only used when no
        # explicit observation exists, and never for records policy retains.
        sc = self._absence_scope(system, rid, cut) if category in SCOPE_ABSENCE_CATEGORIES else None
        if sc is not None:
            return ResourceState(system, rid, category, NOT_FOUND, sc.evidence_id,
                                 sc.observed_at, raw={"scope": sc.copy})
        # No evidence at audit_at. Record whether the resource is known from
        # later evidence (identity link only; never used as state proof).
        return ResourceState(system, rid, category, None,
                             raw={"later_evidence": bool(self.obs_by_rid.get(rid))})

    def _absence_scope(self, system: str, rid: str, cut) -> Scope | None:
        """Latest complete snapshot of `system` at/before cut that omits rid.
        Only snapshots dedicated to the system's own records are trusted:
        if any snapshot from the same evidence file lists rid, it is ignored."""
        best = None
        for s in self.scopes_by_system.get(system, ()):
            t = parse_ts(s.observed_at)
            if parse_ts(s.captured_at) > cut or t > cut:
                continue
            if rid in s.present:
                continue
            same_file = [x for x in self.scopes_by_system[system] if x.evidence_id == s.evidence_id]
            if any(rid in x.present for x in same_file):
                continue
            if best is None or t > parse_ts(best.observed_at):
                best = s
        return best


def _evidence_sort_key(o: Observation):
    # Deterministic choice among simultaneous observations.
    return (o.evidence_id, o.location)


def _best_category(obs, rid: str) -> str | None:
    """Most specific category seen for the resource (explicit evidence beats
    the resource-id prefix)."""
    prefix_cat = category_for_resource(rid)
    cats = [o.category for o in obs if o.category]
    special = [c for c in cats if c != prefix_cat]
    if special:
        return sorted(special)[0]
    return cats[0] if cats else prefix_cat



UNASSIGNED_COST = 1e4      # leaving a case/resource unmatched
IMPOSSIBLE_COST = 1e9      # pair is not a candidate


def _min_cost_assignment(cand) -> dict[str, dict[str, str]]:
    """cand: [(cost, case_id, resource_id, category, activity_id)].
    Within each category, match cases to resources one-to-one, maximising the
    number of matches and then minimising total cost (Hungarian algorithm on
    each connected group of competing cases/resources)."""
    best: dict[tuple, tuple] = {}
    for cost, case_id, rid, cat, aid in cand:
        k = (cat, case_id, rid)
        if k not in best or (cost, aid) < best[k]:
            best[k] = (cost, aid)
    out: dict[str, dict[str, str]] = defaultdict(dict)
    by_cat: dict = defaultdict(list)
    for (cat, case_id, rid), (cost, aid) in best.items():
        by_cat[cat].append((case_id, rid, cost, aid))
    for cat in sorted(by_cat, key=str):
        edges = by_cat[cat]
        parent: dict = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for case_id, rid, _c, _a in edges:
            parent[find(("c", case_id))] = find(("r", rid))
        groups: dict = defaultdict(list)
        for e in edges:
            groups[find(("c", e[0]))].append(e)
        for g in sorted(groups.values(), key=lambda es: min(es)):
            cases = sorted({e[0] for e in g})
            rids = sorted({e[1] for e in g})
            n = max(len(cases), len(rids))
            m = [[UNASSIGNED_COST] * n for _ in range(n)]
            for i in range(len(cases)):
                for j in range(len(rids)):
                    m[i][j] = IMPOSSIBLE_COST
            lookup = {}
            for case_id, rid, cost, aid in g:
                i, j = cases.index(case_id), rids.index(rid)
                m[i][j] = cost
                lookup[(i, j)] = aid
            for i, j in enumerate(_hungarian(m)):
                if i < len(cases) and j < len(rids) and (i, j) in lookup:
                    out[cases[i]][rids[j]] = f"workflow:{lookup[(i, j)]}"
    return dict(out)


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Minimum-cost perfect matching on a square matrix; returns the column
    assigned to each row (Kuhn-Munkres with potentials, O(n^3))."""
    n = len(cost)
    INF = float("inf")
    u, v = [0.0] * (n + 1), [0.0] * (n + 1)
    p, way = [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [INF] * (n + 1), [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    assign = [0] * n
    for j in range(1, n + 1):
        if p[j]:
            assign[p[j] - 1] = j - 1
    return assign


def flagged_cases(direct_by_case: dict[str, dict[str, list[str]]],
                  flags: dict[str, ActivityEvent]) -> dict[str, str]:
    """{case_id: activity_id} for cases whose directly linked account was
    flagged non-deletion by CLASSIFY_REQUEST (see parsers.activity.
    non_deletion_flags). For cross-checking the classifier only."""
    out = {}
    for case_id, links in sorted(direct_by_case.items()):
        for rid in sorted(links):
            if rid in flags:
                out[case_id] = flags[rid].activity_id
    return out
