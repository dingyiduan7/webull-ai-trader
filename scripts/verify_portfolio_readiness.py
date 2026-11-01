"""Explicit closed-market, read-only acceptance. No model/gateway/permit imports."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autonomous_trader.sandbox.secrets import load_credentials
from autonomous_trader.sandbox.certification.sanitize import OrderSanitizer
from autonomous_trader.mvp.context import SandboxSource
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.durable.ledger import LocalDatabase, Ledger
from autonomous_trader.monitoring.opportunities import OpportunityQueue,code
from autonomous_trader.monitoring.store import Store
from zoneinfo import ZoneInfo


def main():
    clock=lambda:datetime.now(timezone.utc)
    session=reviewed_calendar().session(clock().astimezone(ZoneInfo('America/New_York')).date())
    if session and session.contains(clock()):raise ValueError('CLOSED_MARKET_CHECK_ONLY')
    store=Store();runtime=store.runtime()
    if runtime.get('mode')!='SANDBOX':raise ValueError('SANDBOX_REQUIRED')
    ledger=Ledger(LocalDatabase('postgres','m2_writer',schema='mvp_sandbox'))
    state=ledger.snapshot(runtime['account_id'])
    if state['positions'] or any(not o['final'] for o in state['orders'].values()):raise ValueError('FLAT_FINAL_REQUIRED')
    if any(state['latches'].values()):raise ValueError('SAFETY_LATCHED')
    credentials=load_credentials(ROOT);store.check=OrderSanitizer(credentials.sensitive_values()).assert_clean
    source=SandboxSource(credentials)
    observation=source.refresh_portfolio(state) if hasattr(source,'refresh_portfolio') else source.refresh(state,portfolio=True)
    if observation.positions or observation.open_intents:raise ValueError('BROKER_NOT_FLAT_FINAL')
    matches=(observation.settled==Decimal(state['cash']['settled_usd']) and observation.unsettled==Decimal(state['cash']['unsettled_usd'])
        and observation.equity==Decimal(state['cash']['liquidation_equity_usd']))
    if not matches:raise ValueError('BROKER_LOCAL_CASH_MISMATCH')
    queue=OpportunityQueue(store,'SANDBOX_READ_ONLY_PREFLIGHT',clock)
    queue.refill(observation.candidates,clock().date())
    for job in queue.eligible_jobs()[:4]:
        queue.prepare(job)
        try:
            current=source.refresh_focus(state,job['symbol']);item=current.select(job['symbol'],clock())
            if item.instrument_id!=job['instrument_id']:raise ValueError('FOCUSED_INSTRUMENT_IDENTITY_MISMATCH')
            reason='SESSION_CLOSED'
        except Exception as exc:reason=code(exc)
        queue.finish(job,'DROPPED',reason)
    stats=source.discovery_stats
    output={'at':clock().isoformat(),'scope':'CLOSED_MARKET_READ_ONLY_NO_MODEL_NO_ORDER',
        'discovery':{k:stats.get(k) for k in ('observed_rows','unique_symbols','eligible_symbols','rank_sources','unsupported_rank_sources',
            'screen_started_at','screen_completed_at','screen_elapsed_ms','refresh_started_at','refresh_completed_at',
            'refresh_elapsed_ms','discovery_interval_seconds','endpoint_calls_in_window')},
        'shortlist':[{'symbol':x.ticker,'market_value':str(x.market_value) if x.market_value else None,'bid':str(x.bid),
            'sources':x.discovery_sources} for x in observation.candidates],
        'pipeline':queue.health(),'broker_local_balances_match':matches,'broker_positions':0,'broker_open_orders':0,
        'local_reconciliation_ready':state['reconciliation']['ready'],'settlement_known':state['reconciliation']['settlement_known'],
        'any_latch':False,'model_calls':0,'order_writes':0}
    store.emit('PORTFOLIO_READINESS',output,mode='SANDBOX_READ_ONLY_PREFLIGHT',at=clock())
    print(json.dumps(output))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'result':'READ_ONLY_CHECK_FAILED','reason':code(exc)}));sys.exit(1)
