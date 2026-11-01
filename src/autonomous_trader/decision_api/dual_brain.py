"""Deterministic combiner for two decision models (Opus primary, Astra second).

Pure functions, no I/O, no clock, no model calls: both proposals arrive already
validated as AlphaDecision (or None when that model failed). The combiner can
only choose between, or conservatively merge, proposals the models made; it can
never invent a trade, loosen a stop, enlarge a size or turn a hold into a sale.

Rules (owner decisions 2026-10-14/15):
- ENTRY needs consensus: both models must propose OPEN on the same instrument.
  The merged order takes the smaller size, the lower limit, the tighter stop and
  the earlier expiry/review. Any disagreement abstains.
- If one model failed, the other decides alone (degraded, recorded). If both
  failed, there is no entry.
- POSITION review (owner-approved 2026-10-19): a planned sale needs consensus. A
  primary CLOSE is placed only if the second model also says CLOSE; otherwise the
  position is held (stops, deadlines and safety exits are unaffected). A second-
  model CLOSE alone never sells. If the second model failed, the primary decides
  alone (degraded, recorded), as for entries.
- ENTRY merge (owner-approved 2026-10-19): tighter stop, smaller size, lower limit,
  but the later deadline and the higher target. Each model's plan was validated on
  its own; the merge only improves reward-to-risk, so a valid plan stays valid.
"""
from dataclasses import dataclass
from decimal import Decimal

from ..contracts import AlphaDecision, AlphaOpen

PRIMARY = 'primary'
SECOND = 'second'


@dataclass(frozen=True)
class Combined:
    decision: AlphaDecision | None
    mode: str                # CONSENSUS | DEGRADED_PRIMARY_ONLY | DEGRADED_SECOND_ONLY | DISAGREEMENT | BOTH_FAILED
    challenge: bool = False  # second model wanted a CLOSE the primary did not
    note: str = ''


def _abstain(reason):
    return AlphaDecision(action='ABSTAIN', reason=reason[:1000])


def _notional(o: AlphaOpen):
    if o.desired_notional_usd is not None:
        return o.desired_notional_usd
    return o.desired_quantity * o.entry_limit_usd


def combine_entry(primary: AlphaDecision | None, second: AlphaDecision | None) -> Combined:
    if primary is None and second is None:
        return Combined(_abstain('BOTH_MODELS_FAILED: no entry'), 'BOTH_FAILED')
    if second is None:
        return Combined(primary, 'DEGRADED_PRIMARY_ONLY', note='second model failed')
    if primary is None:
        return Combined(second, 'DEGRADED_SECOND_ONLY', note='primary model failed')
    if primary.action != 'OPEN' or second.action != 'OPEN':
        if primary.action == 'ABSTAIN' and second.action == 'ABSTAIN':
            return Combined(primary, 'CONSENSUS', note='both abstain')
        return Combined(_abstain('NO_CONSENSUS: models disagree on whether to open'), 'DISAGREEMENT')
    a, b = primary.open, second.open
    if a.instrument_id != b.instrument_id:
        return Combined(_abstain('NO_CONSENSUS: models chose different instruments'), 'DISAGREEMENT')
    smaller = a if _notional(a) <= _notional(b) else b
    limit = min(a.entry_limit_usd, b.entry_limit_usd)
    stop = max(a.hard_stop_usd, b.hard_stop_usd)
    if not stop < limit:
        return Combined(_abstain('NO_CONSENSUS: tightest stop is not below the lowest limit'), 'DISAGREEMENT')
    targets = [t for t in (a.target_usd, b.target_usd) if t is not None]
    size = ({'desired_quantity': smaller.desired_quantity} if smaller.desired_quantity is not None
            else {'desired_notional_usd': smaller.desired_notional_usd})
    merged = AlphaOpen(
        instrument_id=a.instrument_id, symbol=a.symbol, strategy_label=a.strategy_label, thesis=a.thesis,
        entry_limit_usd=limit, entry_expires_at=min(a.entry_expires_at, b.entry_expires_at),
        hard_stop_usd=stop, latest_exit_at=max(a.latest_exit_at, b.latest_exit_at),
        target_usd=max(targets) if targets else None,
        next_review_at=min(a.next_review_at, b.next_review_at), **size)
    reason = ('CONSENSUS | primary: %s | second: %s' % (primary.reason, second.reason))[:1000]
    return Combined(AlphaDecision(action='OPEN', reason=reason, open=merged), 'CONSENSUS')


def combine_position(primary: AlphaDecision | None, second: AlphaDecision | None) -> Combined:
    if primary is None and second is None:
        return Combined(None, 'BOTH_FAILED', note='position left unchanged')
    if primary is None:
        return Combined(second, 'DEGRADED_SECOND_ONLY', note='primary model failed')
    if second is None:
        return Combined(primary, 'DEGRADED_PRIMARY_ONLY', note='second model failed')
    challenge = second.action == 'CLOSE' and primary.action != 'CLOSE'
    mode = 'CONSENSUS' if primary.action == second.action else 'DISAGREEMENT'
    return Combined(primary, mode, challenge=challenge,
                    note='second model challenges: wants CLOSE' if challenge else '')


def consensus_close(primary: AlphaDecision | None, second: AlphaDecision | None, hold_review_at) -> Combined:
    """Sell-side consensus: CLOSE only when both models say CLOSE. A primary CLOSE the second model does not
    share becomes HOLD (reviewed again at the second model's own review time, else hold_review_at)."""
    from ..contracts import AlphaHold
    if primary is None or primary.action != 'CLOSE':
        return Combined(primary, 'NOT_A_CLOSE')
    if second is None:
        return Combined(primary, 'DEGRADED_PRIMARY_ONLY', note='second model failed')
    if second.action == 'CLOSE':
        reason = ('CONSENSUS CLOSE | primary: %s | second: %s' % (primary.reason, second.reason))[:1000]
        return Combined(primary.model_copy(update={'reason': reason}), 'CONSENSUS')
    review = hold_review_at
    if second.hold is not None:
        review = second.hold.next_review_at
    elif second.modify is not None and second.modify.requested_next_review_at is not None:
        review = second.modify.requested_next_review_at
    reason = ('NO_CLOSE_CONSENSUS: held | primary wanted CLOSE: %s | second: %s %s'
              % (primary.reason, second.action, second.reason))[:1000]
    held = AlphaDecision(action='HOLD', reason=reason,
                         hold=AlphaHold(position_id=primary.close.position_id, next_review_at=review))
    return Combined(held, 'DISAGREEMENT', note='close vetoed by second model')
