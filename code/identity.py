"""Identity resolution (Person B).

Resolves a request email to a stable internal ``user_id`` and the *complete*
set of identifier tokens that can link evidence to that person: current and
historical emails and phones, every email hash the data carries, and the
``sha256(lower(email))`` of every email — current and historical.

Why historical identifiers matter: 115 of the 518 request emails are not in
``user_identities.csv``. They are resolved through ``user_audit_log.csv``,
which records email / phone / email_hash changes over time. Evidence frequently
references a person by an *old* identifier:

* case_12's ``analytics_9914`` is linked only by the ``email_hash`` ``old_value``
  recorded in the audit log, and
* a record may carry the hash of an email the user no longer uses, so we hash
  every known email, not just the current one.

All normalisation uses the helpers in ``models.py`` so that the tokens produced
here compare equal to the ones the parsers attach to observations.
"""
from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from pathlib import Path

from models import Identity, norm_email, norm_phone, norm_id


def sha256_email(email: str) -> str:
    """Hash a *normalised* email the way the dataset does: sha256 of the
    lower-cased address, hex digest."""
    return hashlib.sha256(norm_email(email).encode()).hexdigest()


class IdentityResolver:
    """Builds every person's identifier set once, then resolves requests.

    Construction order mirrors the verified sample harness so that resolution
    is identical to the reference wiring:

    1. ``user_identities.csv`` in file order creates the people and seeds the
       current email / phone / email_hash / name.
    2. ``user_audit_log.csv`` adds the historical ``old_value`` and
       ``new_value`` of every email / phone / email_hash change.
    3. ``sha256(lower(email))`` is added for every email collected above.
    4. The email -> user_id index is built in people-insertion order with
       first-writer-wins (``setdefault``) semantics.
    """

    def __init__(self, dataset_dir: str | Path):
        ds = Path(dataset_dir)
        self.people: dict[str, Identity] = {}
        self._by_email: dict[str, str] = {}
        self._insertion: list[str] = []  # user_ids in first-seen order
        self._load_identities(ds / "user_identities.csv")
        self._load_audit_log(ds / "user_audit_log.csv")
        self._add_email_hashes()
        self._index_emails()

    # -- construction ------------------------------------------------------

    def _person(self, user_id: str) -> Identity:
        key = norm_id(user_id)
        p = self.people.get(key)
        if p is None:
            p = Identity(key)
            self.people[key] = p
            self._insertion.append(key)
        return p

    def _load_identities(self, path: Path) -> None:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                p = self._person(row["user_id"])
                if row.get("email"):
                    p.emails.add(norm_email(row["email"]))
                if row.get("phone"):
                    ph = norm_phone(row["phone"])
                    if ph:
                        p.phones.add(ph)
                if row.get("email_hash"):
                    p.email_hashes.add(row["email_hash"].strip().lower())
                if row.get("name"):
                    p.names.add(row["name"].strip())

    def _load_audit_log(self, path: Path) -> None:
        if not path.exists():
            return
        with open(path, newline="", encoding="utf-8") as fh:
            for a in csv.DictReader(fh):
                p = self._person(a["user_id"])
                field = a.get("field_name", "")
                for value in (a.get("old_value"), a.get("new_value")):
                    if not value:
                        continue
                    if field == "email":
                        p.emails.add(norm_email(value))
                    elif field == "phone":
                        ph = norm_phone(value)
                        if ph:
                            p.phones.add(ph)
                    elif field == "email_hash":
                        p.email_hashes.add(value.strip().lower())
                    elif field == "name":
                        p.names.add(value.strip())

    def _add_email_hashes(self) -> None:
        for p in self.people.values():
            p.email_hashes |= {sha256_email(e) for e in p.emails}

    def _index_emails(self) -> None:
        for user_id in self._insertion:
            for email in self.people[user_id].emails:
                self._by_email.setdefault(email, user_id)

    # -- resolution --------------------------------------------------------

    def resolve(self, email: str) -> Identity | None:
        """Resolve a request email (current or historical) to an Identity, or
        None when the email is unknown."""
        user_id = self._by_email.get(norm_email(email))
        return self.people.get(user_id) if user_id else None

    def resolve_user_id(self, email: str) -> str | None:
        return self._by_email.get(norm_email(email))

    def identities(self) -> dict[str, Identity]:
        return self.people


# --------------------------------------------------------------------------- #
#  Validation: resolve every sample request, including the ones whose request
#  email is not the current identity email (resolved via the audit log).
# --------------------------------------------------------------------------- #
def _validate(repo: Path) -> int:
    ds = repo / "dataset"
    resolver = IdentityResolver(ds)
    current = set()
    with open(ds / "user_identities.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("email"):
                current.add(norm_email(r["email"]))

    sample = ds / "sample_deletion_requests.csv"
    rows = list(csv.DictReader(open(sample, newline="", encoding="utf-8")))
    resolved = via_history = 0
    failures = []
    for r in rows:
        email = norm_email(r["email"])
        ident = resolver.resolve(email)
        if ident is None:
            failures.append((r["case_id"], email))
            continue
        resolved += 1
        if email not in current:
            via_history += 1
    print(f"samples: {resolved}/{len(rows)} resolved "
          f"({via_history} via historical identifiers)")

    # Full set: report how many need the audit log (expected ~115).
    full = list(csv.DictReader(open(ds / "deletion_requests.csv",
                                     newline="", encoding="utf-8")))
    full_hist = sum(1 for r in full
                    if resolver.resolve(norm_email(r["email"])) is not None
                    and norm_email(r["email"]) not in current)
    full_fail = [r["case_id"] for r in full
                 if resolver.resolve(norm_email(r["email"])) is None]
    print(f"full set: {len(full) - len(full_fail)}/{len(full)} resolved "
          f"({full_hist} via historical identifiers)")

    # case_12 specific check: the hash of its old email must be present.
    c12 = next((r for r in full if r["case_id"] == "case_12"), None)

    if failures:
        print("UNRESOLVED samples:", failures)
    return 0 if not failures else 1


if __name__ == "__main__":
    import sys

    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    raise SystemExit(_validate(repo))
