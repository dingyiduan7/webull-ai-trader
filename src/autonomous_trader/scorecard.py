"""Weekly profit scorecard and capital-scaling rule (owner goals 2026-10-16).

Answers, per exchange week: did the account make money after broker fees AND model cost, which strategies
earned it, and is the record strong enough to add capital or time to tighten risk as the account grows.
Pure functions over the ledger state, original plans and model-cost totals; recommendations only: every
limit change still needs the owner's signature.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from .monitoring.views import trades_from_state
from .strategy_allocation import label, weights

# Owner decision 2026-10-16: 10% per position for now; tighten as capital grows ("much higher").
RISK_TIERS = ((Decimal('2000'), Decimal('0.10')), (Decimal('10000'), Decimal('0.05')),
              (Decimal('50000'), Decimal('0.03')), (None, Decimal('0.02')))
ADD_CAPITAL_WEEKS = 4  # consecutive profitable weeks (net of model cost) before suggesting more capital


def week_of(value):
    d = value if isinstance(value, date) and not isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace('Z', '+00:00')).date()
    return d - timedelta(days=d.weekday())


def tier_risk(equity):
    for ceiling, risk in RISK_TIERS:
        if ceiling is None or equity < ceiling:
            return risk
    return RISK_TIERS[-1][1]


def weekly(state, plans, model_cost_by_week):
    """[{week, trades, wins, realized, model_cost, net, by_strategy}] oldest first."""
    trades = trades_from_state(state, {}, plans)
    entries = {}
    for t in trades:
        key = t.get('decision_id') or (t['ticker'], t['entry_time'])
        e = entries.setdefault(key, dict(net=Decimal(0), exit='', strategy=label(t.get('strategy'))))
        e['net'] += Decimal(str(t['net']))
        e['exit'] = max(e['exit'], str(t['exit_time']))
    weeks = {}
    for day, value in state.get('daily_realized', {}).items():
        weeks.setdefault(week_of(date.fromisoformat(day)), dict(realized=Decimal(0), entries=[]))['realized'] += Decimal(value)
    for e in entries.values():
        weeks.setdefault(week_of(e['exit']), dict(realized=Decimal(0), entries=[]))['entries'].append(e)
    for w in model_cost_by_week:
        weeks.setdefault(w, dict(realized=Decimal(0), entries=[]))
    out = []
    for w in sorted(weeks):
        row, cost = weeks[w], Decimal(str(model_cost_by_week.get(w, 0)))
        by = {}
        for e in row['entries']:
            g = by.setdefault(e['strategy'], dict(trades=0, wins=0, net=Decimal(0)))
            g['trades'] += 1; g['wins'] += e['net'] > 0; g['net'] += e['net']
        out.append(dict(week=w.isoformat(), trades=len(row['entries']), wins=sum(e['net'] > 0 for e in row['entries']),
                        realized=row['realized'], model_cost=cost, net=row['realized'] - cost, by_strategy=by))
    return out, trades


def recommendation(weeks, trades, equity, signed_trade_risk):
    """Plain-language next steps from the record; never changes anything by itself."""
    notes = []
    target = tier_risk(equity)
    if target < signed_trade_risk:
        notes.append(f'TIGHTEN_RISK: equity ${equity:.2f} is in the {target * 100:.0f}% tier; the signed per-position '
                     f'risk is {signed_trade_risk * 100:.0f}%. Re-sign the charter with {target * 100:.0f}%.')
    streak = 0
    for w in reversed(weeks):
        if w['trades'] and w['net'] > 0:
            streak += 1
        else:
            break
    total_net = sum((w['net'] for w in weeks), Decimal(0))
    n = sum(w['trades'] for w in weeks)
    if streak >= ADD_CAPITAL_WEEKS and total_net > 0 and n >= 10:
        notes.append(f'ADD_CAPITAL_CANDIDATE: {streak} profitable weeks in a row after model cost, '
                     f'{n} trades, total net ${total_net:.2f}. Evidence supports adding capital in steps.')
    else:
        notes.append(f'KEEP_CAPITAL: {streak}/{ADD_CAPITAL_WEEKS} profitable weeks in a row, {n} closed trades '
                     f'(need {ADD_CAPITAL_WEEKS} weeks and 10+ trades before adding capital).')
    for name, (w, why) in sorted(weights(trades).items()):
        if w < 1:
            notes.append(f'SIZE_REDUCED: {name} at {w * 100:.0f}% size ({why}).')
    if weeks and weeks[-1]['model_cost'] > 0 and weeks[-1]['realized'] > 0 and weeks[-1]['model_cost'] > weeks[-1]['realized'] / 2:
        notes.append('MODEL_COST_HIGH: model spend took more than half of this week\'s trading profit.')
    return notes


def render(weeks, notes, equity):
    lines = [f'AUTO TRADER WEEKLY SCORECARD  (equity ${equity:.2f})', '',
             'week        trades wins  realized   model$     net']
    for w in weeks:
        lines.append(f"{w['week']}  {w['trades']:>5} {w['wins']:>4} {w['realized']:>9.2f} {w['model_cost']:>8.2f} {w['net']:>8.2f}")
        for name, g in sorted(w['by_strategy'].items(), key=lambda kv: -kv[1]['net']):
            lines.append(f"    {name[:40]:<40} {g['trades']:>3} trades {g['wins']:>3} wins {g['net']:>9.2f}")
    total = sum((w['net'] for w in weeks), Decimal(0))
    lines += ['', f'TOTAL NET AFTER MODEL COST: {total:.2f}', ''] + notes
    return '\n'.join(lines)


def render_decisions(evidence, dual, allocations):
    """Today's decisions with the stated reason for each buy or pass (abstain-reason visibility).
    evidence: PORTFOLIO_DECISION_EVIDENCE payloads; dual: REAL_DUAL_BRAIN_DECISION payloads;
    allocations: REAL_STRATEGY_ALLOCATION payloads. All oldest first."""
    lines = [f'DECISIONS ({len(evidence)} entry calls)']
    for e in evidence:
        names = ','.join(c.get('symbol', '?') for c in (e.get('candidates') or ()))
        pick = e.get('selected_symbol') or '-'
        cost = e.get('model_cost_usd')
        lines.append(f"{str(e.get('decision_snapshot_at', ''))[11:19]} {e.get('action', '?'):<7} {pick:<6} "
                     f"${cost if cost is not None else '?'}  [{names}]")
        lines.append('    why: ' + (e.get('stated_reason') or '(no reason recorded)')[:300])
    if dual:
        lines += ['', 'SECOND BRAIN (Astra) ON OPUS BUYS']
        for d in dual:
            lines.append(f"  Opus {d.get('primary_action')} {d.get('primary_instrument') or ''} | Astra {d.get('second_action')}"
                         f" {d.get('second_instrument') or ''} -> {d.get('consensus_action')}"
                         + (f"  (error {d['second_error']})" if d.get('second_error') else ''))
            if d.get('second_reason'):
                lines.append('    Astra why: ' + d['second_reason'][:300])
    for a in allocations:
        lines.append(f"  SIZE {a.get('strategy')}: x{a.get('weight')} ({a.get('reason')}){'' if a.get('placed') else ' -> not placed'}")
    return '\n'.join(lines)
