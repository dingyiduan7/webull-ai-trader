from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from scripts import run_real_trader as trader

@pytest.fixture
def inert(monkeypatch):
    args=NS(port=8766,poll_seconds=30,recovery=True,supervisor_handle='fixture',supervisor_pipe_handle=1,
        sme_advisory_provider=None,sme_advisory_provider_sha256=None,sme_advisory_file=None,
        sme_advisory_sha256=None,sme_authorization_db=None)
    parser=Mock();parser.parse_args.return_value=args
    monkeypatch.setattr(trader.argparse,'ArgumentParser',lambda:parser)
    receipt=dict(build_sha256='build',account_binding='test',market_session_label='2026-10-06')
    consume=Mock(return_value=NS(receipt=receipt));monkeypatch.setattr(trader,'consume_supervisor_handle',consume)
    state=dict(positions={},orders={},latches={},incidents={},reconciliation={'ready':True})
    runner=NS(controller=NS(state=state,recovery_only=True,account_id='test',incident=Mock()),
        authority_receipt=receipt,step=Mock(return_value='CLOSED_RECONCILED'),worker=Mock())
    store=Mock();lock=Mock();compose=Mock(return_value=(runner,store,lock))
    monkeypatch.setattr(trader,'compose_with_heartbeat',compose)
    monitor=Mock();monkeypatch.setattr(trader,'MonitoringHeartbeat',lambda store:monitor)
    stop=Mock();stop.wait.side_effect=[False,True]
    monkeypatch.setattr(trader.threading,'Event',lambda:stop)
    thread_factory=Mock();monkeypatch.setattr(trader.threading,'Thread',thread_factory)
    monkeypatch.setattr(trader,'start_optional_dashboard',Mock(return_value=None))
    beats=[];monkeypatch.setattr(trader,'supervisor_heartbeat',beats.append)
    return NS(runner=runner,store=store,lock=lock,compose=compose,consume=consume,monitor=monitor,beats=beats,
        threads=thread_factory,stop=stop)

def test_initial_recovery_never_advertises_normal_trading(inert):
    trader.main()
    assert inert.beats[0]['status']=='STARTING'
    assert all(b['production_enabled'] is False for b in inert.beats)
    assert inert.beats[-1]['status']=='STOPPED'
    inert.lock.close.assert_called_once();inert.monitor.close.assert_called_once()

def test_initial_recovery_failure_releases_writer(inert):
    inert.runner.step.side_effect=RuntimeError('recovery failed')
    with pytest.raises(RuntimeError,match='recovery failed'):trader.main()
    inert.lock.close.assert_called_once();inert.monitor.close.assert_called_once()
    assert not any(b['production_enabled'] for b in inert.beats)

def test_missing_supervisor_capability_precedes_startup_publication(inert):
    inert.consume.side_effect=ValueError('REAL_SUPERVISOR_PIPE_INVALID')
    with pytest.raises(ValueError,match='REAL_SUPERVISOR_PIPE_INVALID'):trader.main()
    inert.compose.assert_not_called()
    assert inert.beats==[]

def test_failed_current_authority_cannot_produce_running_heartbeat(inert):
    inert.compose.side_effect=ValueError('REAL_OWNER_AUTHORIZATION_REVOKED')
    with pytest.raises(ValueError,match='REAL_OWNER_AUTHORIZATION_REVOKED'):trader.main()
    assert not inert.beats

def test_late_pulse_cannot_overwrite_terminal_heartbeat(inert):
    trader.main()
    final=list(inert.beats)
    pulse=inert.threads.call_args.kwargs['target']
    inert.stop.wait.side_effect=[False,True]
    pulse()
    assert inert.beats==final and final[-1]['status']=='STOPPED'
