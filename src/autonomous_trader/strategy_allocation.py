"""Strategy-weighted sizing (owner goal 2026-10-16: scale profits, favor what works).

Capital follows evidence: a new entry's size is scaled by its strategy's realized REAL record. The weight
only ever shrinks a position (1 = the model's own size, which the signed hard limits already bound); it can
never raise risk above the owner-signed charter. Exploration stays at full size until a strategy has enough
closed trades to judge. Pure functions; the runner applies the weight before the order is built.
"""
from decimal import Decimal, ROUND_FLOOR

MIN_JUDGED_TRADES = 5          # below this a record is anecdotal: no penalty
COLD_STREAK = 3                # this many consecutive losses halves the size until a win
LOSING_WEIGHT = Decimal('0.5')  # net-negative record with >= MIN_JUDGED_TRADES
FAILING_WEIGHT = Decimal('0.25')  # net-negative, >= 10 trades and win rate below 35%
FLOOR_WEIGHT = Decimal('0.25')


def label(value):
    return (value or 'unlabeled').strip().lower()[:60]


def records(trades):
    """Closed entries per strategy, oldest first: {label: [net, ...]} (FIFO pieces of one entry merged)."""
    by_entry = {}
    for t in trades:
        key = t.get('decision_id') or (t.get('ticker'), t.get('entry_time'))
        row = by_entry.setdefault(key, dict(strategy=label(t.get('strategy')), net=Decimal(0), exit=''))
        row['net'] += Decimal(str(t['net']))
        row['exit'] = max(row['exit'], str(t.get('exit_time') or ''))
    out = {}
    for row in sorted(by_entry.values(), key=lambda r: r['exit']):
        out.setdefault(row['strategy'], []).append(row['net'])
    return out


def weight(history):
    """Size weight in [FLOOR_WEIGHT, 1] for one strategy's ordered net results, with the reason."""
    n = len(history)
    if not n:
        return Decimal(1), 'NEW_STRATEGY_FULL_SIZE'
    w, why = Decimal(1), 'RECORD_OK'
    net, wins = sum(history, Decimal(0)), sum(1 for v in history if v > 0)
    if n >= 10 and net < 0 and Decimal(wins) / n < Decimal('0.35'):
        w, why = FAILING_WEIGHT, 'FAILING_RECORD'
    elif n >= MIN_JUDGED_TRADES and net < 0:
        w, why = LOSING_WEIGHT, 'LOSING_RECORD'
    elif n < MIN_JUDGED_TRADES:
        why = 'ANECDOTAL_FULL_SIZE'
    if n >= COLD_STREAK and all(v <= 0 for v in history[-COLD_STREAK:]) and w > LOSING_WEIGHT:
        w, why = LOSING_WEIGHT, 'COLD_STREAK'
    return max(w, FLOOR_WEIGHT), why


def weights(trades):
    return {name: weight(history) for name, history in records(trades).items()}


def scale_open(open_proposal, w, increment=Decimal(1)):
    """Return the proposal with its size scaled by w (never up); None when it rounds to zero shares."""
    if w >= 1:
        return open_proposal
    if open_proposal.desired_quantity is not None:
        q = (open_proposal.desired_quantity * w / increment).to_integral_value(ROUND_FLOOR) * increment
        return open_proposal.model_copy(update={'desired_quantity': q}) if q > 0 else None
    notional = (open_proposal.desired_notional_usd * w).quantize(Decimal('0.01'), ROUND_FLOOR)
    if notional < open_proposal.entry_limit_usd * increment:
        return None
    return open_proposal.model_copy(update={'desired_notional_usd': notional})
