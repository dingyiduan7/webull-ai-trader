from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from psycopg import sql

from autonomous_trader.durable.ledger import LocalDatabase
from autonomous_trader.real_readiness import account_readiness
from autonomous_trader.real_state import (provision, RealObservationLedger, binding,
    reduce_observation, DisabledRealExecution)
from autonomous_trader.real_portfolio import RealCandidate, prepare_portfolio_context
from autonomous_trader.contracts import AlphaInstrumentContext
from autonomous_trader.mvp.config import reviewed_calendar

NOW = datetime(2026, 9, 29, 15, tzinfo=timezone.utc)
ACCOUNT = binding('synthetic-real-account')


def observation(at=NOW):
    return account_readiness({'account_currency_assets': [{'currency': 'USD',
        'settled_cash': '500', 'unsettled_cash': '0', 'cash_balance': '500',
        'market_value': '5.44', 'net_liquidation_value': '505.44'}]},
        [{'symbol': s, 'quantity': q, 'last_price': p, 'market_value': v}
         for s,q,p,v in [('ICLN','.05675','16.81','.95'), ('SMH','.00188','600.01','1.13'),
                        ('ESGU','.00719','167.38','1.20'), ('PHO','.01733','68.63','1.19'),
                        ('LIT','.01408','68.28','.96')]], {'data': []}, at)


@pytest.fixture
def ledger():
    db = LocalDatabase('postgres', 'm2_admin', schema='real_readiness_test_'+uuid4().hex[:12])
    provision(db)
    try: yield RealObservationLedger(db)
    finally:
        with db.connect() as c:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(db.schema)))


def test_real_rounding_is_exact_evidence_without_cash_or_basis_adjustment():
    o = observation()
    assert o.account_totals_match and o.display_rounding_proven
    assert o.display_rounding_difference_usd == Decimal('.01')
    assert o.settled_usd == 500 and o.equity_usd == Decimal('505.44')
    bad = o.model_dump(mode='json')
    bad['external_holdings'][0]['quantity'] = '.1'
    # Changing quantity alone cannot prove a new rounding explanation.
    rows = [{'symbol':p.symbol,'quantity':str(p.quantity),'market_value':str(p.market_value_usd),
             'last_price':'1'} for p in o.external_holdings]
    result = account_readiness({'account_currency_assets':[{'currency':'USD','settled_cash':'500',
        'unsettled_cash':'0','cash_balance':'500','market_value':'5.44','net_liquidation_value':'505.44'}]}, rows, {'data':[]}, NOW)
    assert not result.display_rounding_proven and not result.account_totals_match


def test_namespace_account_binding_and_disabled_adapter(ledger):
    with pytest.raises(ValueError, match='REAL_NAMESPACE_REQUIRED'):
        RealObservationLedger(LocalDatabase('postgres','m2_admin',schema='mvp_sandbox'))
    state = ledger.reconcile(ACCOUNT, observation(), now=NOW)
    assert state['managed_positions'] == state['reservations'] == {}
    assert not state['entry_authority'] and state['observation_reconciled']
    adapter = DisabledRealExecution(ledger, ACCOUNT)
    with pytest.raises(ValueError, match='PRODUCTION_DISABLED'): adapter.execution_adapter()
    assert not hasattr(adapter, 'place') and not hasattr(adapter, 'cancel')
    with pytest.raises(ValueError, match='REAL_ACCOUNT_BINDING_MISMATCH'):
        ledger.reconcile(binding('different'), observation(NOW+timedelta(seconds=1)), now=NOW+timedelta(seconds=1))
    assert ledger.snapshot()['version'] == 1


def test_restart_replay_append_only_and_external_change_latches(ledger):
    ledger.reconcile(ACCOUNT, observation(), now=NOW)
    restarted = RealObservationLedger(ledger.db)
    assert restarted.verify_replay() == ledger.snapshot()
    changed = observation(NOW+timedelta(seconds=1)).model_copy(update={'external_holdings':()})
    state = restarted.reconcile(ACCOUNT, changed, now=NOW+timedelta(seconds=1))
    assert state['incident_latched'] and not state['observation_reconciled']
    state = restarted.reconcile(ACCOUNT, observation(NOW+timedelta(seconds=2)), now=NOW+timedelta(seconds=2))
    assert state['incident_latched']  # No silent latch reset on the next good read.
    assert restarted.verify_replay() == state
    with pytest.raises(Exception, match='APPEND_ONLY_REAL_JOURNAL'):
        with ledger.db.connect() as c: c.execute('DELETE FROM observation_journal')


def test_concurrent_writers_and_stale_observations(ledger):
    def write(_):
        try: return ledger.reconcile(ACCOUNT, observation(), now=NOW)['version']
        except ValueError as exc:
            assert str(exc)=='REAL_OBSERVATION_NOT_NEWER'
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(write,range(4)))
    assert results.count(1)==1 and ledger.verify_replay()['version']==1
    with pytest.raises(ValueError,match='STALE'):
        ledger.reconcile(ACCOUNT,observation(),now=NOW+timedelta(seconds=31))


def candidate(symbol='AAA',price='20',**kw):
    item=AlphaInstrumentContext(instrument_id='stock-'+symbol,symbol=symbol,quote_at=NOW,
        bid_usd=str(Decimal(price)-Decimal('.01')),ask_usd=price,
        price_increment_usd='0.01',quantity_increment='1',volume='1000000')
    args=dict(context=item,session_label='2026-09-29',received_at=NOW,host='api.webull.com',
        instrument_certified=True,minimum_order_quantity=Decimal(1),entry_cost_allowance_usd=Decimal('.10'))
    args.update(kw)
    return RealCandidate(**args)


def test_real_small_account_portfolio_packet_uses_existing_contract_without_call(ledger):
    ledger.reconcile(ACCOUNT,observation(),now=NOW)
    values=[candidate('LOW','1'),candidate('HIGH','600'),candidate('ICLN','16.81'),
            candidate('OLD','20',session_label='2026-09-28')]
    context,excluded=prepare_portfolio_context(ledger,ACCOUNT,values,reviewed_calendar().session(NOW.date()),NOW)
    assert [i.symbol for i in context.instruments]==['LOW']
    assert {r['reason'] for r in excluded}=={'MINIMUM_ORDER_UNAFFORDABLE','EXTERNAL_HOLDING_NO_AUTHORITY','PRIOR_SESSION_CANDIDATE'}
    assert context.settled_cash_usd==500 and not context.reconciliation_ready
    assert context.positions==() and context.remaining_risk_usd==0
    assert 'daily_research' in context.instruments[0].market_context
    assert 'entry_cost_allowance_usd' in context.instruments[0].market_context


def test_real_packet_never_uses_sandbox_stale_or_uncertified_quote(ledger):
    ledger.reconcile(ACCOUNT,observation(),now=NOW)
    session=reviewed_calendar().session(NOW.date())
    with pytest.raises(ValueError,match='REAL_MARKET_PROVENANCE_REQUIRED'):
        prepare_portfolio_context(ledger,ACCOUNT,[candidate(host='api.sandbox.webull.com')],session,NOW)
    for c in (candidate(instrument_certified=False),candidate(received_at=NOW-timedelta(seconds=3)),
              candidate(entry_cost_allowance_usd=None)):
        with pytest.raises(ValueError,match='NO_ELIGIBLE'):
            prepare_portfolio_context(ledger,ACCOUNT,[c],session,NOW)


def test_real_shortlist_is_bounded_and_preserves_quality_evidence(ledger):
    ledger.reconcile(ACCOUNT,observation(),now=NOW)
    values=[candidate('S'+str(i)) for i in range(20)]
    context,_=prepare_portfolio_context(ledger,ACCOUNT,values,reviewed_calendar().session(NOW.date()),NOW)
    assert len(context.instruments)==8
