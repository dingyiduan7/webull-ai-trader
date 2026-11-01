"""When the REAL trader spends a model call on entry research.

Claude is called only at decision points. A new portfolio-entry call is
justified by one of:

* FIRST_OF_SESSION       - no entry call yet this session;
* COST_EVIDENCE_CHANGED  - the execution-cost evidence supplied to the model changed;
* NEW_CANDIDATE          - an instrument entered the shortlist since the last call;
* MATERIAL_MOVE          - any shortlisted mid price moved >= MATERIAL_MOVE from the
                           mid the model last saw (hysteresis: the reference only
                           resets when a call is made, so tick flicker around a
                           bucket edge can never re-trigger);
* HEARTBEAT              - HEARTBEAT has elapsed since the last call (slow drift).

MIN_INTERVAL (2026-10-20): no entry call within 15 minutes of the previous one, for any reason. On 2026-10-20
the 1% move rule on volatile gap-up names fired a paid call every ~100 s (~70 calls, $5.42, all ABSTAIN) and
exhausted the daily model budget by 9:40 MT. Holding reviews and protective exits are not entry calls.

Everything else (quote ticks, receipt-clock changes, one-cent spread flicker)
is not new decision evidence. Spend is independently bounded by the model
budget's rolling call limit and daily spend ceiling.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json

MATERIAL_MOVE = Decimal('0.03')          # was 1%: intraday noise on volatile names
HEARTBEAT = timedelta(minutes=30)
MIN_INTERVAL = timedelta(minutes=15)
MAX_SHORTLIST = 8


def reference(context):
    """Compact evidence the next trigger decision compares against."""
    mids = {q.instrument_id: str((Decimal(q.bid_usd) + Decimal(q.ask_usd)) / 2) for q in context.instruments}
    cost = sha256((context.execution_cost_evidence or '').encode('utf-8')).hexdigest()
    return {'mids': mids, 'cost_evidence_sha256': cost}


def entry_trigger(last, current, now):
    """Return a trigger reason, or None when no new call is justified.

    last: {'at': iso, 'reference': reference(...)} of the previous entry call, or None.
    """
    if not last:
        return 'FIRST_OF_SESSION'
    if now - datetime.fromisoformat(last['at']) < MIN_INTERVAL:
        return None
    before = last['reference']
    if before.get('cost_evidence_sha256') != current['cost_evidence_sha256']:
        return 'COST_EVIDENCE_CHANGED'
    if set(current['mids']) - set(before.get('mids', {})):
        return 'NEW_CANDIDATE'
    for iid, mid in current['mids'].items():
        old = Decimal(before['mids'][iid])
        if old > 0 and abs(Decimal(mid) / old - 1) >= MATERIAL_MOVE:
            return 'MATERIAL_MOVE'
    if now - datetime.fromisoformat(last['at']) >= HEARTBEAT:
        return 'HEARTBEAT'
    return None


def trigger_token(reason, now):
    return json.dumps({'reason': reason, 'at': now.isoformat()}, separators=(',', ':'))
