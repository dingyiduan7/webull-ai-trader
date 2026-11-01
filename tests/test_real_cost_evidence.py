from autonomous_trader.real_market import REAL_COST_EVIDENCE
from test_real_daily import fixture,NOW


def test_bounded_cost_context_distinguishes_expense_from_reserve(tmp_path,monkeypatch):
    _,_,bundle=fixture(tmp_path,monkeypatch)
    packet,_,_=bundle(NOW)
    context=packet.context.model_copy(update={'execution_cost_evidence':REAL_COST_EVIDENCE})
    type(context).model_validate(context.model_dump())
    assert len(REAL_COST_EVIDENCE)<=2000
    for text in ('2026-10-02','documented $0','standard NORMAL','LIMIT',
            'https://developer.webull.com/apis/docs/faq/','https://www.webull.com/pricing',
            'unknown is not zero','ADR/depositary','slippage remains uncertain',
            'model costs','internal conservative cash/risk stress reserve',
            'NOT expected charged broker expense','does not change ledger accounting or reserves'):
        assert text in REAL_COST_EVIDENCE


def test_corrected_economic_evidence_is_new_work_but_quote_ticks_are_not(tmp_path,monkeypatch):
    from dataclasses import replace
    from autonomous_trader.real_runner import portfolio_context_key
    from autonomous_trader.mvp.config import reviewed_calendar
    _,_,bundle=fixture(tmp_path,monkeypatch);packet,_,_=bundle(NOW)
    session=reviewed_calendar().session(NOW.date())
    before=portfolio_context_key('account',session,packet)
    corrected=replace(packet,context=packet.context.model_copy(update={'execution_cost_evidence':REAL_COST_EVIDENCE}))
    after=portfolio_context_key('account',session,corrected)
    assert before!=after
    tick=corrected.context.instruments[0].model_copy(update={'bid_usd':corrected.context.instruments[0].bid_usd+__import__('decimal').Decimal('.01')})
    changed=replace(corrected,context=corrected.context.model_copy(update={'instruments':(tick,)}))
    assert portfolio_context_key('account',session,changed)==after


def test_used_half_session_marker_does_not_consume_unused_atomic_allowance(tmp_path,monkeypatch):
    from test_real_packet_handoff import setup
    runner,packet,starts,events,checks=setup(tmp_path,monkeypatch)
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,query,params):self.key=params[0];return self
        def fetchone(self):return (1,) if self.key.startswith('real-portfolio-slot-') else None
    runner.store.db.connect=Connection
    assert runner._wait_for_market_data()=='PORTFOLIO_EVALUATION'
    assert len(starts)==1
