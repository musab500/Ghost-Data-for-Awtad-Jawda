"""Custom case 2: non-deletion requests must classify as NOT_APPLICABLE.

Access / copy / correction / marketing-opt-out / login-help / keep-account
requests are not erasure, even when they use words like "remove". Genuine
erasure requests must still classify as deletions. Uses the deterministic
fallback so the test is offline and reproducible.
"""
from classifier import fallback_classify

NON_DELETION = [
    "Please stop the promotional emails. I still need the account, so I am not asking for deletion.",
    "I'd like to download my account information in a portable format. Please keep the account as it is.",
    "My surname is misspelled. Can you correct it without removing any of my records?",
    "Please remove me from marketing lists only. I still want receipts.",
    "Can you tell me what personal information you hold about me? This is an information request, not a request to erase it.",
    "I can't access my profile and need help resetting the login. Do not close the account.",
]
DELETION = [
    "Please delete my account and erase all of my personal data.",
    "I deleted this account but the same email is still recognized. Please remove any remaining copy of my profile.",
    "I invoke my right to be forgotten; wipe my data from all your systems.",
]


def run():
    bad = []
    for t in NON_DELETION:
        if fallback_classify(t).is_deletion:
            bad.append(("should be NA", t[:50]))
    for t in DELETION:
        if not fallback_classify(t).is_deletion:
            bad.append(("should be deletion", t[:50]))
    ok = not bad
    detail = (f"{len(NON_DELETION)} non-deletion + {len(DELETION)} deletion texts classified correctly"
              if ok else f"misclassified: {bad}")
    return ("non_deletion_classifier", ok, detail)


if __name__ == "__main__":
    print(run())
