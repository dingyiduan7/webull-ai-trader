import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autonomous_trader.contracts import AlphaContext,AlphaInstrumentContext
from autonomous_trader.decision_api.astra_probe import stamp_alpha_decision
from autonomous_trader.decisions import parse_alpha_decision
from autonomous_trader.monitoring.decision_evidence import (
    build_portfolio_decision_evidence,persist_portfolio_decision_evidence,
)
from autonomous_trader.monitoring.store import Store,monthly_model_cost


def context(now,*,future_quote=False):
    quote=now+timedelta(seconds=1) if future_quote else now-timedelta(seconds=1)
    item=AlphaInstrumentContext(instrument_id='stock-a',symbol='AAA',quote_at=quote,
        bid_usd='99.99',ask_usd='100',price_increment_usd='0.01',quantity_increment='1',
        reference_price_usd='99.995',spread_bps='1.0001',volume='1000000',
        market_context=json.dumps({'market_provenance':'HTTP_SNAPSHOT','bounded':'evidence'}))
    return AlphaContext(as_of=now,expires_at=now+timedelta(seconds=30),
        session_open=now-timedelta(minutes=30),session_close=now+timedelta(hours=6),
        liquidation_at=now+timedelta(hours=5,minutes=55),instruments=(item,),settled_cash_usd='100000',
        remaining_risk_usd='5000',reconciliation_ready=True,settlement_known=True)


def raw(action,now):
    value={'schema_version':'1.0','action':action,'reason':
        ('No candidate has supportable positive expected net value.' if action=='ABSTAIN' else 'Continuation evidence supports entry.'),
        'open':None,'hold':None,'modify':None,'close':None}
    if action=='OPEN':
        value['open']={'instrument_id':'stock-a','symbol':'AAA','strategy_label':'quality continuation',
            'thesis':'Quality and price evidence support an exploratory long.','desired_notional_usd':None,
            'desired_quantity':'10','entry_limit_usd':'100','entry_expires_at':(now+timedelta(minutes=2)).isoformat(),
            'hard_stop_usd':'95','latest_exit_at':(now+timedelta(hours=2)).isoformat(),
            'target_usd':'110','next_review_at':(now+timedelta(minutes=30)).isoformat()}
    return value


def build(action,now,*,cost='0.12',created_offset=2,future_quote=False):
    c=context(now,future_quote=future_quote);decision=parse_alpha_decision(json.dumps(raw(action,now)))
    envelope=stamp_alpha_decision(decision,c,created_at=now+timedelta(seconds=created_offset),
        policy_version='policy',capability_version='capability',model='gpt-6-astra',reasoning='medium',
        usage={'input_tokens':100,'output_tokens':20},estimated_cost_usd=cost or '0')
    ai={'model_work_id':'claim-1','latency_ms':2000,'estimated_cost_usd':cost}
    return c,decision,envelope,ai


def test_abstain_record_is_bounded_and_preserves_unavailable_fields(now):
    c,d,e,ai=build('ABSTAIN',now);record=build_portfolio_decision_evidence(c,d,e,ai)
    assert record.action=='ABSTAIN' and record.selected_symbol is None and record.entry_exit_plan is None
    assert record.regime_label=='UNCLASSIFIED'
    assert record.stated_prediction==d.reason and record.stated_horizon_end is None
    assert record.candidates[0].quote_source_id=='HTTP_SNAPSHOT'
    assert 'sector_context' in record.candidates[0].unavailable_fields
    assert record.decision_snapshot_at==now and record.model_response_at==now+timedelta(seconds=2)
    payload=record.model_dump(mode='json')
    assert 'prompt' not in payload and 'credentials' not in json.dumps(payload).lower()


def test_open_record_joins_decision_to_order_outcome_and_cost_once(now):
    c,d,e,ai=build('OPEN',now);record=build_portfolio_decision_evidence(c,d,e,ai)
    assert record.selected_symbol=='AAA' and record.entry_exit_plan==d.open
    assert record.decision_id==record.order_intent_plan_id==record.outcome_plan_id==e.decision_id
    assert record.episode_id=='alpha-stock-a'
    assert record.model_work_claim_id==record.model_work_result_id=='claim-1'
    assert record.model_cost_usd==Decimal(ai['estimated_cost_usd']) and record.model_cost_status=='FINAL'
    assert record.cost_authority=='REFERENCE_ONLY_MODEL_WORK_RESULT_IS_CANONICAL'
    assert record.candidates[0].quote_at==now-timedelta(seconds=1)


def test_missing_final_model_cost_remains_unknown(now):
    c,d,e,ai=build('ABSTAIN',now,cost=None)
    record=build_portfolio_decision_evidence(c,d,e,ai)
    assert record.model_cost_usd is None and record.model_cost_status=='UNKNOWN'


def test_evidence_cost_copy_is_reference_only_and_not_double_counted(now):
    c,d,e,ai=build('OPEN',now);record=build_portfolio_decision_evidence(c,d,e,ai)
    rows=[('MODEL_WORK_CLAIM',{'id':'claim-1','reserved_usd':'0.20'}),
        ('MODEL_WORK_RESULT',{'id':'claim-1','cost_usd':'0.12','ai':{'model_work_id':'claim-1',
            'estimated_cost_usd':'0.12','cost_authority':'BILLABLE_WORK'}}),
        ('PORTFOLIO_DECISION_EVIDENCE',record.model_dump(mode='json'))]
    cost=monthly_model_cost(rows)
    assert cost['known']==Decimal('0.12') and cost['reserved']==0 and not cost['unbounded']


class Result:
    def __init__(self,row):self.row=row
    def fetchone(self):return self.row


class Connection:
    def __init__(self,rows):self.rows=rows
    def execute(self,statement,params):
        sql=' '.join(str(statement).split()).upper()
        if sql.startswith('INSERT'):
            key=params[0]
            if key in self.rows:return Result(None)
            payload=getattr(params[4],'obj',params[4]);self.rows[key]=(params[2],params[3],payload)
            return Result((key,))
        if sql.startswith('SELECT KIND,MODE,PAYLOAD'):
            return Result(self.rows.get(params[0]))
        raise AssertionError(statement)


class ConnectionContext:
    def __init__(self,rows):self.connection=Connection(rows)
    def __enter__(self):return self.connection
    def __exit__(self,*_):return None


class Database:
    def __init__(self):self.rows={}
    def connect(self):return ConnectionContext(self.rows)


def persistent_store(database):
    store=Store.__new__(Store);store.db=database;store.check=lambda _:None
    return store


def test_restart_replay_is_idempotent_and_conflicts_fail(now):
    c,d,e,ai=build('ABSTAIN',now);record=build_portfolio_decision_evidence(c,d,e,ai);database=Database()
    event_id=persist_portfolio_decision_evidence(persistent_store(database),record,mode='DRY_RUN',at=e.created_at)
    assert persist_portfolio_decision_evidence(persistent_store(database),record,mode='DRY_RUN',at=e.created_at)==event_id
    assert len(database.rows)==1
    changed=record.model_copy(update={'stated_prediction':'Different result'})
    with pytest.raises(ValueError,match='MONITOR_EVENT_IDEMPOTENCY_CONFLICT'):
        persist_portfolio_decision_evidence(persistent_store(database),changed,mode='DRY_RUN',at=e.created_at)


def test_temporal_causality_rejects_future_input_and_labels_late_result(now):
    c,d,e,ai=build('ABSTAIN',now,future_quote=True)
    with pytest.raises(ValueError,match='PORTFOLIO_FUTURE_EVIDENCE'):
        build_portfolio_decision_evidence(c,d,e,ai)
    c,d,e,ai=build('ABSTAIN',now,created_offset=31)
    record=build_portfolio_decision_evidence(c,d,e,ai)
    assert not record.response_within_cutoff and record.decision_cutoff_at==now+timedelta(seconds=30)
