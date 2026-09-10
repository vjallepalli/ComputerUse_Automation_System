"""In-memory seed data for the fake credit-union back office.

No database, no real PII. Names / member IDs / SSN-format strings are obviously
fake (SSNs use the never-issued 9xx area range). Mutated in place when a
sub-account is opened during a run; `reset()` (POST /debug/reset) or a process
restart restores the pristine seed.
"""

from __future__ import annotations

import copy

# One seeded member is flagged restricted -> looking them up or acting on them
# is a permission-denied path. One is marked slow -> its detail page sleeps, to
# exercise a wait/timeout condition later. Both are load-bearing for replay.
RESTRICTED_MEMBER_ID = "10004"
SLOW_MEMBER_ID = "10002"
SLOW_LOAD_SECONDS = 2.0

MEMBERS: dict[str, dict] = {
    "10001": {
        "member_id": "10001",
        "name": "Dorothy Q. Fentiman",
        "ssn": "912-04-5510",
        "phone": "(555) 0100-118",
        "status": "active",
        "accounts": [
            {"number": "CHK-10001-01", "type": "Checking", "balance": 2841.19},
            {"number": "SAV-10001-01", "type": "Savings", "balance": 15220.00},
        ],
    },
    "10002": {
        "member_id": "10002",
        "name": "Marcus Auil Brenneke",
        "ssn": "912-55-8830",
        "phone": "(555) 0100-204",
        "status": "active",
        "accounts": [
            {"number": "CHK-10002-01", "type": "Checking", "balance": 190.42},
        ],
    },
    "10003": {
        "member_id": "10003",
        "name": "Pearl Kowalczyk-Ade",
        "ssn": "912-18-2247",
        "phone": "(555) 0100-771",
        "status": "active",
        "accounts": [
            {"number": "CHK-10003-01", "type": "Checking", "balance": 6605.00},
            {"number": "SAV-10003-01", "type": "Savings", "balance": 812.55},
            {"number": "MMK-10003-01", "type": "Money Market", "balance": 44010.10},
        ],
    },
    "10004": {
        "member_id": "10004",
        "name": "Cornelius Vandergriff",
        "ssn": "912-77-3301",
        "phone": "(555) 0100-999",
        "status": "restricted",
        "accounts": [
            {"number": "CHK-10004-01", "type": "Checking", "balance": 0.00},
        ],
    },
    "10005": {
        "member_id": "10005",
        "name": "Ingrid Selassie Boothroyd",
        "ssn": "912-33-1092",
        "phone": "(555) 0100-350",
        "status": "active",
        "accounts": [
            {"number": "SAV-10005-01", "type": "Savings", "balance": 3300.75},
        ],
    },
    "10006": {
        "member_id": "10006",
        "name": "Horace P. Blitzendorf",
        "ssn": "912-90-4415",
        "phone": "(555) 0100-616",
        "status": "active",
        "accounts": [
            {"number": "CHK-10006-01", "type": "Checking", "balance": 78.00},
            {"number": "SAV-10006-01", "type": "Savings", "balance": 129400.23},
        ],
    },
}


# Explicit per-member sub-account counter. NOT derived from len(accounts): the
# ID must increment for real (survive an account being removed) and must fall
# back to 01 after reset(). Lives on the member dict so reset() clears it too.
for _m in MEMBERS.values():
    _m.setdefault("sub_seq", 0)

# Pristine snapshot for reset(). Taken after sub_seq is seeded.
_PRISTINE: dict[str, dict] = copy.deepcopy(MEMBERS)


def reset() -> None:
    """Restore the seed to its pristine state (undo sub-accounts opened this run)."""
    MEMBERS.clear()
    MEMBERS.update(copy.deepcopy(_PRISTINE))


def get_member(member_id: str) -> dict | None:
    """Look up a member by exact ID. Returns None if there is no such member."""
    return MEMBERS.get((member_id or "").strip())


def next_sub_account_number(member: dict) -> str:
    """Advance the member's sub-account counter and return SUB-<id>-<NN>."""
    member["sub_seq"] += 1
    return f"SUB-{member['member_id']}-{member['sub_seq']:02d}"
