"""Print the weekly profit scorecard and today's decisions with reasons (read-only; never a broker client or trader).

Usage: python scripts/weekly_scorecard.py      -> prints and saves .local/scorecard/latest.txt
"""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def collect(db, now):
    from psycopg import sql
    from autonomous_trader.durable.projection import digest
    from autonomous_trader.monitoring.store import Store, monthly_model_cost
    from autonomous_trader.reference import NEW_YORK
    from autonomous_trader.scorecard import week_of
    with db.connect() as c:
        c.execute('SET TRANSACTION READ ONLY')
        row = c.execute('SELECT payload FROM runtime WHERE id=1').fetchone()
        runtime = row[0] if row else {}
        aid = runtime.get('account_id')
        if runtime.get('ledger_schema') != 'real_trader' or not aid:
            raise SystemExit('SCORECARD_NO_REAL_ACCOUNT')
        row = c.execute(sql.SQL('SELECT state,state_hash FROM {}.accounts WHERE account_id=%s').format(
            sql.Identifier('real_trader')), (aid,)).fetchone()
        if not row or digest(row[0]) != row[1]:
            raise SystemExit('SCORECARD_CHECKPOINT_INTEGRITY')
        state = row[0]
        plans = {r[0]['plan_id']: r[0]['plan'] for r in c.execute(
            "SELECT payload FROM events WHERE mode='REAL' AND kind='REAL_PLAN' AND payload->>'original'='true'").fetchall()}
        days = sorted(state.get('daily_realized', {}))
        first = week_of(datetime.fromisoformat(days[0]).date()) if days else week_of(now.astimezone(NEW_YORK).date())
        costs, w = {}, first
        while w <= now.astimezone(NEW_YORK).date():
            start = datetime.combine(w, time(0), NEW_YORK)
            costs[w] = monthly_model_cost(Store._monthly_budget_rows(c, start, start + timedelta(days=7), ('REAL',)))['known']
            w += timedelta(days=7)
        since = datetime.combine(now.astimezone(NEW_YORK).date(), time(0), NEW_YORK)
        today = {k: [r[0] for r in c.execute(
            "SELECT payload FROM events WHERE mode='REAL' AND kind=%s AND at>=%s ORDER BY at", (k, since)).fetchall()]
            for k in ('PORTFOLIO_DECISION_EVIDENCE', 'REAL_DUAL_BRAIN_DECISION', 'REAL_STRATEGY_ALLOCATION')}
    return state, plans, costs, today


def main(db=None, now=None, out_dir=None):
    from autonomous_trader.durable.ledger import LocalDatabase
    from autonomous_trader.economic_policy import effective_policy
    from autonomous_trader.scorecard import recommendation, render, render_decisions, weekly
    now = now or datetime.now(timezone.utc)
    db = db or LocalDatabase('postgres', 'mvp_reader', schema='mvp_monitor')
    state, plans, costs, today = collect(db, now)
    weeks, trades = weekly(state, plans, costs)
    equity = Decimal(state['cash']['liquidation_equity_usd'])
    text = render(weeks, recommendation(weeks, trades, equity, effective_policy(state).trade_risk), equity)
    text += '\n\n' + render_decisions(today['PORTFOLIO_DECISION_EVIDENCE'], today['REAL_DUAL_BRAIN_DECISION'],
                                      today['REAL_STRATEGY_ALLOCATION'])
    folder = Path(out_dir or ROOT / '.local' / 'scorecard'); folder.mkdir(parents=True, exist_ok=True)
    (folder / 'latest.txt').write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
