"""Incident reasons that the flat-account recovery may clear (one list for every gate).

Clearing still requires the full recovery proof in each gate: no positions, reservations or open/unknown
orders, two fresh exact broker observations equal to the ledger cash, and a clean ledger RECONCILE. The
ledger reconciliation mismatch and held-position monitor failure are included because a flat account whose
cash matches exactly has, by that proof, nothing left to mismatch or monitor (owner-approved 2026-10-16:
Thursday's ~130 such incidents blocked Friday's start with the account already flat).
"""
FLAT_RECOVERABLE_REASONS = frozenset({
    'REAL_ACCOUNT_RECONCILIATION_FAILED',
    'REAL_RECOVERY_RECONCILIATION_FAILED',
    'REAL_POST_CLOSE_CASH_SETTLEMENT_MISMATCH',
    'RECONCILIATION_MISMATCH',
    'REAL_HELD_POSITION_MONITOR_FAILED',
    # Owner-approved 2026-10-19: a read that could not complete (Webull rate limit / account read failure at the
    # close) is not a mismatch; the flat recovery still needs two exact broker observations before it clears.
    'REAL_READ_RATE_LIMITED',
    'REAL_POST_CLOSE_ACCOUNT_READ_FAILED',
})

# Read/monitor failures that may clear while positions are held. Owner approval 2026-10-19: "I approve
# letting the incident lock clear while positions are held, when two fresh Webull readings match the ledger
# exactly on shares and cash." Clearing needs two fresh distinct broker observations whose positions equal
# the ledger's exactly, no open/unknown orders or reservations on either side, cash equal to the ledger, and
# a clean ledger RECONCILE. Mismatch reasons are excluded: they stay incidents while anything is held.
HELD_RECOVERABLE_REASONS = frozenset({
    'REAL_ACCOUNT_RECONCILIATION_FAILED',
    'REAL_RECOVERY_RECONCILIATION_FAILED',
    'REAL_HELD_POSITION_MONITOR_FAILED',
})
