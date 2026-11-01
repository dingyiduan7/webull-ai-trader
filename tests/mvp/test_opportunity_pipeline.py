"""Portfolio queue containment and the unchanged single-writer authority boundary."""
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta
from decimal import Decimal as D
from threading import Event
from types import SimpleNamespace
import pytest

from test_cycle import NOW,setup_cycle
from test_alpha_reset import observation,universe,open_raw
from autonomous_trader.monitoring.opportunities import (HOT_REFRESH_SECONDS,HOT_RETRY_SECONDS,
    HOT_PROMOTION_MAX_WAIT_SECONDS,OpportunityQueue)
from autonomous_trader.monitoring.runner import Engine,next_discovery_due,portfolio_call_slot,portfolio_shortlist
from autonomous_trader.mvp.config import RuntimeConfig,ExecutionMode
from autonomous_trader.mvp.alpha import build_alpha_context
from autonomous_trader.durable.models import Protection
from autonomous_trader.durable.projection import Rejected
from autonomous_trader.research.portfolio import SessionPortfolioResearch


class Store:
    def __init__(self):self.events=[];self.saved=None
    def runtime(self):return {}
    def recent_outcomes(self):return ()
    def pipeline_state(self,mode):return deepcopy(self.saved)
    def emit(self,kind,payload,**kw):
        self.events.append((kind,deepcopy(payload),kw))
        if kind=='OPPORTUNITY_QUEUE':self.saved=deepcopy(payload)


class ManualExecutor:
    def __init__(self):self.tasks=[]
    def submit(self,work):
        f=Future();self.tasks.append((f,work));return f


def test_full_discovery_cadence_is_start_anchored_and_never_overlaps():
    assert next_discovery_due(NOW,NOW+timedelta(milliseconds=4523))==NOW+timedelta(seconds=15)
    # A slow pass cannot cause overlap; its completion becomes the next due time.
    assert next_discovery_due(NOW,NOW+timedelta(seconds=16))==NOW+timedelta(seconds=16)


def queue_fixture(workers=2):
    current={'at':NOW};store=Store();executor=ManualExecutor()
    queue=OpportunityQueue(store,'DRY_RUN',lambda:current['at'],workers=workers,executor=executor)
    queue.refill([observation('small-a','SMALL'),observation('small-b','MICRO'),observation('other','OTHER')],NOW.date())
    return queue,store,executor,current


def result(decision=None):
    return {'decision':decision or {'schema_version':'1.0','action':'ABSTAIN','reason':'No economic edge.',
        'open':None,'hold':None,'modify':None,'close':None},'model':'gpt-6-astra','reasoning':'medium',
        'latency_ms':5,'usage':{'input_tokens':100,'output_tokens':50},'estimated_cost_usd':'0.01',
        'model_work_id':'test-portfolio-claim'}


def test_nonmajor_low_price_candidates_not_gated_by_empty_deep_focus():
    q,_,_,_=queue_fixture()
    cheap=replace(observation('penny','TINY',bid='.39'),market_value=D('1500000'))
    q.refill([cheap,observation('mid','MID')],NOW.date(),priorities=[])
    assert {j['symbol'] for j in q.eligible_jobs()}=={'TINY','MID'}


def test_portfolio_shortlist_is_bounded_and_excludes_prior_session_candidates():
    q,_,_,_=queue_fixture();jobs=q.eligible_jobs()
    session=SimpleNamespace(label=NOW.date())
    assert [j['symbol'] for j in portfolio_shortlist(jobs,session,limit=2)]==['SMALL','MICRO']
    jobs[0]['session']=(NOW.date()-timedelta(days=1)).isoformat()
    assert [j['symbol'] for j in portfolio_shortlist(jobs,session)]==['MICRO','OTHER']


def test_portfolio_call_has_two_session_slots_and_none_outside_entry_window():
    session=SimpleNamespace(opens_at=NOW-timedelta(minutes=30),liquidation_at=NOW+timedelta(hours=6),
        permits_entry=lambda at:NOW-timedelta(minutes=30)<=at<NOW+timedelta(hours=6))
    assert portfolio_call_slot(session,NOW)==0
    assert portfolio_call_slot(session,NOW+timedelta(hours=4))==1
    assert portfolio_call_slot(session,NOW+timedelta(hours=7)) is None


def test_portfolio_slot_claim_is_restart_durable_and_no_replay():
    q,store,_,_=queue_fixture();assert q.claim_once('portfolio_entry|2026-09-11|0','first')
    restored=OpportunityQueue(store,'DRY_RUN',lambda:NOW,executor=ManualExecutor())
    assert not restored.claim_once('portfolio_entry|2026-09-11|0','changed')


def test_renewed_top_runner_outranks_attempt_fairness_without_removing_broad_jobs():
    q,_,_,current=queue_fixture();jobs={j['symbol']:j for j in q.jobs.values()}
    jobs['SMALL']['attempts']=11;jobs['MICRO']['attempts']=0
    promotions=q.promote_hot([{'symbol':'SMALL','screen_at':NOW.isoformat(),'percent_per_minute':'3.2'}])
    eligible=q.eligible_jobs()
    assert promotions[0]['materially_renewed'] and eligible[0] is jobs['SMALL']
    assert {j['symbol'] for j in eligible}=={'SMALL','MICRO','OTHER'}
    q.finish(jobs['SMALL'],'COMPLETED','ABSTAIN',cooldown=300)
    current['at']+=timedelta(seconds=10)
    q.promote_hot([{'symbol':'SMALL','screen_at':current['at'].isoformat(),'percent_per_minute':'4.1'}])
    assert q.eligible_jobs()[0] is jobs['SMALL']


def test_session_dossier_preserves_prior_abstain_and_executable_side_follow_through():
    q,_,_,current=queue_fixture();job=next(j for j in q.jobs.values() if j['symbol']=='SMALL')
    first=replace(observation('small-a','SMALL',bid='10'),ask=D('10.02'),bid_size=D('100'),ask_size=D('100'),
        relative_volume_10d=D('4'),short_window_change_ratio=D('.12'),short_window_minutes=3)
    q.record_observation(job,first,NOW,regime_label='WEAK_MARKET',screen_evidence={'rank':1})
    q.record_decision(job,'ABSTAIN','Reconsider if continuation persists above 10.20.',NOW)
    current['at']+=timedelta(minutes=3)
    later=replace(first,bid=D('11'),ask=D('11.02'),quote_at=current['at'],received_at=current['at'],
        quote_received_at=current['at'],short_window_change_ratio=D('.20'))
    q.record_observation(job,later,current['at'],regime_label='WEAK_MARKET',screen_evidence={'rank':1})
    dossier=q.dossier(job,current['at']);prior=dossier['prior_astra_decisions'][0]
    assert len(dossier['quote_sequence'])==2 and prior['later_observations']==1
    assert D(prior['best_executable_return_bps'])==((D('11')-D('10.02'))/D('10.02')*10000)
    assert dossier['quote_sequence'][-1]['regime_label']=='WEAK_MARKET'
    assert dossier['first_detected_at']==job['first_at']
    assert dossier['latest_screen_evidence']=={'rank':1}
    q.finish(job,'COMPLETED','ABSTAIN',cooldown=300)
    q.promote_hot([{'symbol':'SMALL','screen_at':current['at'].isoformat(),'percent_per_minute':'5'}])
    assert q.eligible_jobs()[0] is job


def test_multi_hour_weak_market_runner_stays_visible_and_stronger_evidence_reopens_review():
    q,_,executor,current=queue_fixture(workers=1);job=next(j for j in q.jobs.values() if j['symbol']=='SMALL')
    base=replace(observation('small-a','SMALL',bid='6.20'),relative_volume_10d=D('3.8'),
        short_window_change_ratio=D('.12'),short_window_minutes=3,bid_size=D('100'),ask_size=D('100'))
    for hour,(bid,strength) in enumerate((('6.20','2'),('8.05','3'),('10.31','4'),('12.55','5'))):
        current['at']=NOW+timedelta(hours=hour)
        promoted=q.promote_hot([{'symbol':'SMALL','screen_at':current['at'].isoformat(),
            'percent_per_minute':strength}])
        assert promoted and promoted[0]['materially_renewed'] and q.eligible_jobs()[0] is job
        observed=replace(base,bid=D(bid),ask=D(bid)+D('.01'),quote_at=current['at'],received_at=current['at'],
            quote_received_at=current['at'],short_window_change_ratio=D('.12')+D('.04')*hour)
        q.record_observation(job,observed,current['at'],regime_label='BROAD_MARKET_WEAK',screen_evidence={'rank':1})
        q.record_decision(job,'ABSTAIN',f'Hour {hour}: reassess if continuation strengthens.',current['at'])
        q.finish(job,'COMPLETED','ABSTAIN',cooldown=300)
    dossier=q.dossier(job,current['at'])
    assert [row['bid'] for row in dossier['quote_sequence']]==['6.20','8.05','10.31','12.55']
    assert len(dossier['prior_astra_decisions'])==4 and all(
        row['action']=='ABSTAIN' for row in dossier['prior_astra_decisions'])
    assert not executor.tasks  # Visibility/reconsideration evidence never forces or authorizes a BUY.


def test_hot_unchanged_context_rechecks_without_a_second_paid_call():
    q,_,executor,current=queue_fixture();job=next(j for j in q.jobs.values() if j['symbol']=='SMALL')
    q.promote_hot([{'symbol':'SMALL','screen_at':NOW.isoformat(),'percent_per_minute':'3'}])
    assert q.submit(job,None,'same',lambda:None,unchanged_cooldown=HOT_REFRESH_SECONDS)
    executor.tasks[0][0].set_result(result());q.completed();q.finish(job,'COMPLETED','ABSTAIN',cooldown=HOT_REFRESH_SECONDS)
    current['at']+=timedelta(seconds=31)
    before=len(q.calls)
    assert not q.submit(job,None,'same',lambda:None,unchanged_cooldown=HOT_REFRESH_SECONDS)
    assert len(q.calls)==before and job['reason']=='UNCHANGED_CONTEXT'
    assert datetime.fromisoformat(job['not_before'])<=current['at']+timedelta(seconds=30)


def test_hot_stale_refresh_does_not_starve_other_candidates(monkeypatch,tmp_path):
    from test_operational_soak import focus_engine
    e,_,session,_,current=focus_engine(monkeypatch,tmp_path,[ValueError('STALE_MARKET_OR_ACCOUNT'),True,True])
    q=e._queue();q.refill([observation('focus-id','FOCUS'),observation('other-id','OTHER')],session.label)
    q.promote_hot([{'symbol':'FOCUS','screen_at':NOW.isoformat(),'percent_per_minute':'4'}])
    calls=[];original=e.source.refresh_focus
    def counted(state,symbol):
        calls.append(symbol);return original(state,symbol)
    e.source.refresh_focus=counted
    e._run_candidates(session)
    hot=next(j for j in q.jobs.values() if j['symbol']=='FOCUS')
    assert not calls and hot['status']=='PENDING' and hot['reason']=='HOT_EXACT_REFRESH_RETRY'
    assert q.hot_retry_wait_until()==NOW+timedelta(seconds=HOT_RETRY_SECONDS)
    assert q.health()['hot_watch']['last_promotion_to_first_focus_seconds']==0
    current['at']+=timedelta(seconds=HOT_RETRY_SECONDS)
    e._run_candidates(session)
    assert not calls and hot['status'] in ('RUNNING','COMPLETED')
    assert hot['first_exact_refresh_completed_at']==NOW.isoformat()


def test_due_broad_discovery_cannot_starve_active_hot_exact_retry(monkeypatch,tmp_path):
    from test_operational_soak import focus_engine
    e,_,session,_,current=focus_engine(monkeypatch,tmp_path,[True])
    e.controller.state['session']['label']=session.label.isoformat()
    q=e._queue();q.refill([observation('focus-id','FOCUS')],session.label)
    q.promote_hot([{'symbol':'FOCUS','screen_at':NOW.isoformat(),'percent_per_minute':'4'}])
    e.next_discovery=NOW;e.next_poll=NOW+timedelta(seconds=30);broad=[]
    e.source.refresh_portfolio=lambda *_:(broad.append(True) or (_ for _ in ()).throw(AssertionError('broad scan starved hot retry')))
    e._alpha_step(current['at'],session)
    job=next(iter(q.jobs.values()))
    assert not broad and job.get('exact_refresh_completed_at')==NOW.isoformat()
    assert job['status'] in ('RUNNING','COMPLETED')


def test_sandbox_broad_discovery_runs_without_blocking_hot_refresh(monkeypatch,tmp_path):
    from test_operational_soak import focus_engine
    e,_,session,_,current=focus_engine(monkeypatch,tmp_path,[True]);e.config=RuntimeConfig(mode=ExecutionMode.SANDBOX)
    state=e.controller.state;state['session']['label']=session.label.isoformat();state['account_id']='test'
    state['cash'].update(unsettled_usd='0',reserved_usd='0')
    q=e._queue();q.refill([observation('focus-id','FOCUS')],session.label)
    q.promote_hot([{'symbol':'FOCUS','screen_at':NOW.isoformat(),'percent_per_minute':'4'}])
    pending=Future();submitted=[];queue_calls=[]
    class DiscoveryExecutor:
        def submit(self,work,*args):submitted.append((work,args));return pending
    e.discovery_executor=DiscoveryExecutor();e.next_discovery=NOW;e.next_poll=NOW+timedelta(seconds=30)
    e._run_candidates=lambda _:queue_calls.append('hot')
    e._alpha_step(current['at'],session)
    assert len(submitted)==1 and not pending.done()
    assert queue_calls==['hot'] and e.discovery_pending is pending


def test_second_hot_gets_first_focus_before_first_hot_retry_or_ordinary_candidate(monkeypatch,tmp_path):
    from test_operational_soak import focus_engine
    e,_,session,_,current=focus_engine(monkeypatch,tmp_path,[ValueError('STALE_MARKET_OR_ACCOUNT'),True])
    q=e._queue();q.refill([observation('focus-id','FOCUS'),observation('other-id','OTHER'),
        observation('third-id','THIRD')],session.label)
    q.promote_hot([{'symbol':'FOCUS','screen_at':NOW.isoformat(),'percent_per_minute':'4'},
        {'symbol':'OTHER','screen_at':NOW.isoformat(),'percent_per_minute':'3'}])
    calls=[];original=e.source.refresh_focus
    def counted(state,symbol):
        calls.append(symbol)
        if symbol=='OTHER':current['at']+=timedelta(seconds=HOT_RETRY_SECONDS+1)
        return original(state,symbol)
    e.source.refresh_focus=counted
    e._run_candidates(session)
    assert not calls
    jobs={j['symbol']:j for j in q.jobs.values()}
    assert jobs['FOCUS']['reason']=='HOT_EXACT_REFRESH_RETRY'
    assert jobs['OTHER']['status']=='PENDING'
    assert jobs['THIRD']['status']=='PENDING'
    assert q.hot_retry_wait_until()==NOW+timedelta(seconds=HOT_RETRY_SECONDS) and q.hot_retry_pending()
    assert q.eligible_jobs()[0]['symbol']=='OTHER'
    health=q.health()['hot_watch']
    assert health['last_promotion_to_first_focus_seconds']==0
    assert health['last_first_focus_at']==NOW.isoformat()
    assert health['last_model_started_at'] is None


def test_quote_expiring_during_reconciliation_never_starts_paid_model_work(monkeypatch,tmp_path):
    from test_operational_soak import focus_engine
    e,store,session,_,current=focus_engine(monkeypatch,tmp_path,[True])
    q=e._queue();q.refill([observation('focus-id','FOCUS')],session.label)
    original=e.controller.refresh
    def slow_reconciliation(value):
        state=original(value);current['at']+=timedelta(seconds=3);return state
    e.controller.refresh=slow_reconciliation
    e._run_candidates(session)
    job=next(iter(q.jobs.values()))
    assert job['status']=='DROPPED' and job['reason']=='STALE_MARKET_OR_ACCOUNT'
    assert not q.calls and not q.futures
    failures=[payload for kind,payload,_ in store.events if kind=='CANDIDATE_MARKET_DATA_FAILURE']
    assert failures[-1]['stage']=='PRE_MODEL_DISPATCH' and failures[-1]['source_age_ms']==3000


def test_fresh_quote_after_reconciliation_still_starts_model_work(monkeypatch,tmp_path):
    from test_operational_soak import focus_engine
    e,_,session,_,_=focus_engine(monkeypatch,tmp_path,[True])
    q=e._queue();q.refill([observation('focus-id','FOCUS')],session.label)
    e._run_candidates(session)
    job=next(iter(q.jobs.values()))
    assert job['status']=='RUNNING' and len(q.calls)==1 and len(q.futures)==1


def test_completed_model_is_handled_before_another_blocking_refresh_can_queue_delay_it(monkeypatch,tmp_path):
    from test_operational_soak import focus_engine
    e,_,session,_,_=focus_engine(monkeypatch,tmp_path,[True,True])
    q=e._queue();executor=ManualExecutor();q.executor=executor
    q.refill([observation('focus-id','FOCUS'),observation('other-id','OTHER')],session.label)
    calls=[];original=e.source.refresh_focus
    def completing_refresh(state,symbol):calls.append(symbol);return original(state,symbol)
    e.source.refresh_focus=completing_refresh
    e._run_candidates(session)
    jobs={j['symbol']:j for j in q.jobs.values()}
    assert not calls and len(executor.tasks)==1
    executor.tasks[0][0].set_result(result());e._run_candidates(session)
    assert jobs['FOCUS']['status']=='COMPLETED' and jobs['FOCUS']['reason']=='ABSTAIN'
    assert jobs['OTHER']['status']=='PENDING' and len(executor.tasks)==1


def test_hot_retry_expires_after_second_stale_read_without_model_authority(monkeypatch,tmp_path):
    from test_operational_soak import focus_engine
    e,_,session,_,current=focus_engine(monkeypatch,tmp_path,[ValueError('STALE_MARKET_OR_ACCOUNT')]*2)
    q=e._queue();q.refill([observation('focus-id','FOCUS')],session.label)
    q.promote_hot([{'symbol':'FOCUS','screen_at':NOW.isoformat(),'percent_per_minute':'4'}])
    e._run_candidates(session);current['at']+=timedelta(seconds=HOT_RETRY_SECONDS);e._run_candidates(session)
    job=next(iter(q.jobs.values()))
    assert job['reason']=='HOT_EXACT_REFRESH_EXPIRED' and job['status']=='DROPPED'
    assert not q.futures and not q.calls and not q.health()['hot_watch']['symbols']


def test_delayed_hot_exact_refresh_cannot_start_model_after_promotion_expiry(monkeypatch,tmp_path):
    from test_operational_soak import focus_engine
    e,_,session,_,current=focus_engine(monkeypatch,tmp_path,[True])
    q=e._queue();q.refill([observation('focus-id','FOCUS')],session.label)
    q.promote_hot([{'symbol':'FOCUS','screen_at':NOW.isoformat(),'percent_per_minute':'4'}])
    original=e.source.refresh_focus
    def delayed(state,symbol):
        current['at']+=timedelta(seconds=HOT_PROMOTION_MAX_WAIT_SECONDS)
        return original(state,symbol)
    e.source.refresh_focus=delayed
    e._run_candidates(session)
    job=next(iter(q.jobs.values()))
    assert job['reason']=='HOT_PROMOTION_EXPIRED' and job['status']=='DROPPED'
    assert not q.futures and not q.calls and job not in q.eligible_jobs()


def test_pending_hot_retry_and_budget_survive_restart():
    q,store,_,current=queue_fixture();job=next(j for j in q.jobs.values() if j['symbol']=='SMALL')
    q.promote_hot([{'symbol':'SMALL','screen_at':NOW.isoformat(),'percent_per_minute':'4'}])
    q.prepare(job);assert q.retry_hot_refresh(job,'STALE_MARKET_OR_ACCOUNT')
    q.calls=[NOW.isoformat()]*q.calls_per_window;q.save()
    restored=OpportunityQueue(store,'DRY_RUN',lambda:current['at'],executor=ManualExecutor())
    assert restored.hot_retry_wait_until()==NOW+timedelta(seconds=HOT_RETRY_SECONDS)
    assert restored.capacity()==0
    current['at']+=timedelta(seconds=HOT_RETRY_SECONDS)
    assert restored.eligible_jobs()[0]['symbol']=='SMALL'
    assert restored.capacity()==0


def test_aged_completed_hot_membership_does_not_rewrite_model_disposition():
    q,store,executor,current=queue_fixture();job=next(j for j in q.jobs.values() if j['symbol']=='SMALL')
    q.promote_hot([{'symbol':'SMALL','screen_at':NOW.isoformat(),'percent_per_minute':'4'}])
    epoch=job['hot_epoch_id']
    assert q.submit(job,None,'evaluated',lambda:None)
    executor.tasks[0][0].set_result(result());q.completed();q.finish(job,'COMPLETED','ABSTAIN')
    current['at']+=timedelta(seconds=HOT_PROMOTION_MAX_WAIT_SECONDS)
    q.expire_overdue_hot()
    assert job['status']=='COMPLETED' and job['reason']=='ABSTAIN'
    assert not q.health()['hot_watch']['symbols']
    assert job['hot_epoch_blocked_signal_at']==NOW.isoformat()
    assert not any(j is job for j in q.eligible_jobs())
    assert q.aux['last_hot_trace']['hot_epoch_id']==epoch
    assert q.health()['hot_watch']['last_promotion_to_model_start_seconds']==0
    # A changed focused context alone cannot resurrect the expired promotion.
    current['at']+=timedelta(seconds=300)
    q.refill([observation('small-a','SMALL')],NOW.date())
    assert not any(j is job for j in q.eligible_jobs())
    restored=OpportunityQueue(store,'DRY_RUN',lambda:current['at'],executor=ManualExecutor())
    restored_job=next(j for j in restored.jobs.values() if j['symbol']=='SMALL')
    assert restored_job['hot_epoch_blocked_signal_at']==NOW.isoformat()
    assert restored_job not in restored.eligible_jobs()
    # Only a newer broad-screen signal creates a fresh authority epoch.
    assert restored.promote_hot([{'symbol':'SMALL','screen_at':current['at'].isoformat(),
        'percent_per_minute':'4.1'}])[0]['materially_renewed']
    assert restored_job in restored.eligible_jobs()
    assert restored_job['hot_epoch_id']!=epoch and 'hot_epoch_blocked_signal_at' not in restored_job
    assert restored.health()['hot_watch']['last_promotion_to_model_start_seconds']==0


def test_same_screen_signal_cannot_renew_expired_hot_epoch():
    q,_,_,current=queue_fixture();job=next(j for j in q.jobs.values() if j['symbol']=='SMALL')
    signal=NOW.isoformat();q.promote_hot([{'symbol':'SMALL','screen_at':signal,'percent_per_minute':'4'}])
    q.expire_hot(job,'HOT_PROMOTION_EXPIRED')
    current['at']+=timedelta(seconds=5)
    assert q.promote_hot([{'symbol':'SMALL','screen_at':signal,'percent_per_minute':'9'}])==[]
    assert job not in q.eligible_jobs() and job['hot_epoch_blocked_signal_at']==signal


def test_same_screen_signal_does_not_reset_active_hot_promotion_epoch():
    q,_,_,current=queue_fixture();job=next(j for j in q.jobs.values() if j['symbol']=='SMALL')
    signal=NOW.isoformat();first=q.promote_hot([{'symbol':'SMALL','screen_at':signal,'percent_per_minute':'4'}])
    epoch=job['hot_epoch_id'];promoted_at=job['hot_promoted_at'];until=job['hot_until'];revision=q.revision
    current['at']+=timedelta(seconds=30)
    assert q.promote_hot([{'symbol':'SMALL','screen_at':signal,'percent_per_minute':'8'}])==[]
    assert first[0]['hot_epoch_id']==epoch
    assert (job['hot_epoch_id'],job['hot_promoted_at'],job['hot_until'])==(epoch,promoted_at,until)
    assert q.revision==revision


def test_hot_watch_replacement_is_bounded_and_latency_trace_never_crosses_promotions():
    q,_,_,current=queue_fixture();jobs={j['symbol']:j for j in q.jobs.values()}
    q.promote_hot([
        {'symbol':'SMALL','screen_at':NOW.isoformat(),'percent_per_minute':'4'},
        {'symbol':'MICRO','screen_at':NOW.isoformat(),'percent_per_minute':'3'}])
    jobs['SMALL'].update(model_started_at=(NOW+timedelta(seconds=5)).isoformat(),
        model_handled_at=(NOW+timedelta(seconds=12)).isoformat())
    current['at']+=timedelta(seconds=20)
    q.promote_hot([
        {'symbol':'OTHER','screen_at':current['at'].isoformat(),'percent_per_minute':'5'},
        {'symbol':'MICRO','screen_at':current['at'].isoformat(),'percent_per_minute':'4'}])
    health=q.health()['hot_watch']
    assert health['symbols']==['OTHER','MICRO']
    assert health['last_promotion_to_model_start_seconds']==5
    assert health['last_promotion_to_verdict_seconds']==12


def test_restart_expires_extra_hot_members_and_discards_incoherent_persisted_trace():
    q,store,_,current=queue_fixture()
    for rank,job in enumerate(q.jobs.values(),1):
        job.update(hot_rank=rank,hot_until=(NOW+timedelta(minutes=10)).isoformat(),
            hot_signal_at=NOW.isoformat(),hot_promoted_at=(NOW+timedelta(seconds=30)).isoformat(),
            model_started_at=(NOW+timedelta(seconds=10)).isoformat())
    q.save()
    restored=OpportunityQueue(store,'DRY_RUN',lambda:current['at'],executor=ManualExecutor())
    health=restored.health()['hot_watch']
    assert len(health['symbols'])==2
    assert health['last_promotion_to_model_start_seconds'] is None
    assert health['last_promotion_to_verdict_seconds'] is None


def test_result_after_context_expiry_is_audit_only_and_never_ready():
    q,store,executor,current=queue_fixture();job=next(iter(q.jobs.values()))
    context=SimpleNamespace(expires_at=NOW+timedelta(seconds=20))
    assert q.submit(job,context,'late',lambda:None)
    current['at']+=timedelta(seconds=21);executor.tasks[0][0].set_result(result(open_raw(NOW,'small-a','SMALL')))
    assert q.completed()==[]
    assert job['status']=='QUARANTINED' and job['reason']=='MODEL_RESULT_STALE_NO_AUTHORITY'
    discarded=[p for k,p,_ in store.events if k=='ALPHA_DECISION']
    assert discarded[-1]['disposition']=='LATE_RESULT_NO_AUTHORITY'


def test_independent_model_failure_does_not_block_success_or_other_jobs():
    q,store,executor,_=queue_fixture();a,b,c=list(q.jobs.values())
    assert q.submit(a,None,'a',lambda:None) and q.submit(b,None,'b',lambda:None)
    executor.tasks[0][0].set_exception(ValueError('INVALID_ALPHA_DECISION'))
    executor.tasks[1][0].set_result(result())
    ready=q.completed()
    assert a['status']=='DROPPED' and a['reason']=='INVALID_ALPHA_DECISION'
    assert ready[0][0] is b and b['status']=='RESULT_READY'
    q.finish(b,'COMPLETED','ABSTAIN')
    assert q.submit(c,None,'c',lambda:None)
    assert len(executor.tasks)==3


def test_slow_candidate_does_not_block_other_completed_result():
    q,_,executor,_=queue_fixture();a,b,_=list(q.jobs.values())
    q.submit(a,None,'a',lambda:None);q.submit(b,None,'b',lambda:None)
    executor.tasks[1][0].set_result(result())
    assert q.completed()[0][0] is b
    assert a['status']=='RUNNING'


def test_watchdog_quarantines_and_does_not_replay_or_reclaim_running_thread():
    q,_,executor,current=queue_fixture();a,b,_=list(q.jobs.values())
    q.submit(a,None,'a',lambda:None)
    current['at']+=timedelta(seconds=91);assert q.completed()==[]
    assert a['reason']=='MODEL_DEADLINE_NO_REPLAY'
    assert q.health()['worker_health']=='DEGRADED' and q.capacity()==1
    assert q.submit(b,None,'b',lambda:None)
    executor.tasks[0][0].set_result(result(open_raw(NOW,'small-a','SMALL')))
    assert q.completed()==[] and a['status']=='QUARANTINED'
    assert not any(x[0]=='BROKER_RESULT' for x in q.store.events)


def test_durable_claim_precedes_model_work_and_restart_preserves_cooldown_dedup():
    q,store,executor,current=queue_fixture();job=list(q.jobs.values())[0]
    class Verify:
        def submit(self,work):
            assert store.saved['jobs'][job['key']]['status']=='RUNNING'
            assert store.saved['jobs'][job['key']]['fingerprints']==['fingerprint']
            return Future()
    q.executor=Verify();q.submit(job,None,'fingerprint',lambda:None)
    restored=OpportunityQueue(store,'DRY_RUN',lambda:current['at'],executor=ManualExecutor())
    recovered=restored.jobs[job['key']]
    assert recovered['status']=='QUARANTINED' and recovered['reason']=='ABANDONED_JOB_NO_REPLAY'
    assert recovered not in restored.eligible_jobs()
    current['at']+=timedelta(minutes=6)
    assert not restored.submit(recovered,None,'fingerprint',lambda:None)
    assert recovered['reason']=='UNCHANGED_CONTEXT'
    assert not restored.executor.tasks


def test_rolling_call_budget_survives_restart():
    q,store,executor,current=queue_fixture()
    for j in q.jobs.values():
        assert q.submit(j,None,j['key'],lambda:None)
        executor.tasks[-1][0].set_result(result());q.completed();q.finish(j,'COMPLETED','ABSTAIN')
    assert q.capacity()==0
    other=OpportunityQueue(store,'DRY_RUN',lambda:current['at'],executor=ManualExecutor())
    assert other.capacity()==0
    current['at']+=timedelta(seconds=301);assert other.capacity()==2


def test_prior_session_jobs_are_inert_until_fresh_session_discovery():
    q,store,executor,current=queue_fixture();old_jobs=list(q.jobs.values())
    assert q.eligible_jobs()
    current['at']+=timedelta(days=1)
    restored=OpportunityQueue(store,'DRY_RUN',lambda:current['at'],executor=ManualExecutor())
    assert not restored.eligible_jobs()
    assert all(not restored.jobs[job['key']]['current'] for job in old_jobs)
    assert restored.calls==[] and restored.futures=={}

    fresh=observation('small-a','SMALL')
    restored.refill([fresh],current['at'].date())
    eligible=restored.eligible_jobs()
    assert len(eligible)==1 and eligible[0]['session']==str(current['at'].date())
    assert eligible[0]['key'] not in {job['key'] for job in old_jobs}


def test_background_fingerprint_claim_survives_restart():
    q,store,_,current=queue_fixture();assert q.claim_aux('deep','snapshot')
    other=OpportunityQueue(store,'DRY_RUN',lambda:current['at'],executor=ManualExecutor())
    assert not other.claim_aux('deep','snapshot')
    assert other.claim_aux('deep','changed')


def test_deep_planning_with_advancing_clock_and_no_retry_record(setup_cycle,tmp_path):
    e,_,_,context,_,_=engine_fixture(setup_cycle,tmp_path)
    current={'at':NOW}
    def ticking_clock():
        current['at']+=timedelta(microseconds=1)
        return current['at']
    e.clock=ticking_clock
    submitted=[]
    e.planning_executor=SimpleNamespace(submit=lambda *args:(submitted.append(args) or Future()))
    e.brain_factory=lambda:SimpleNamespace(deep_plan=lambda context:None)
    e._start_planning(context)
    assert len(submitted)==1 and 'deep' in e.background
    e._start_planning(context)
    assert len(submitted)==1  # In-flight planning cannot be duplicated.


def test_deep_planning_retry_and_unchanged_context_remain_cost_gates(setup_cycle,tmp_path):
    e,_,_,context,_,_=engine_fixture(setup_cycle,tmp_path)
    submitted=[]
    e.planning_executor=SimpleNamespace(submit=lambda *args:(submitted.append(args) or Future()))
    e.brain_factory=lambda:SimpleNamespace(deep_plan=lambda context:None)
    e.background_retry['deep']=NOW+timedelta(seconds=30)
    e._start_planning(context)
    assert not submitted
    e.clock=lambda:NOW+timedelta(seconds=30)
    e._start_planning(context)
    assert len(submitted)==1
    e.background.clear()
    e.clock=lambda:NOW+timedelta(hours=1)
    e._start_planning(context)
    assert len(submitted)==1  # Persisted fingerprint prevents a timer-only call.


def engine_fixture(setup_cycle,tmp_path):
    args,broker,ledger=setup_cycle;store=Store();e=Engine(tmp_path,RuntimeConfig(),store,clock=lambda:NOW)
    e.controller=args['controller'];e.gateway=args['gateway'];e.alpha_manager=SimpleNamespace(plans=lambda _: {})
    q=e._queue();e.pipeline_outcomes=()
    items=[observation('iid-'+str(n),'SYM'+chr(65+n)) for n in range(6)]
    initial=universe(items);e.controller.refresh(initial)
    context=build_alpha_context(initial,e.controller.state,NOW)
    q.refill(items,NOW.date());versions=[]
    class Source:
        def refresh_focus(self,state,symbol):
            versions.append(state['version'])
            item=next(x for x in items if x.ticker==symbol)
            # Exact current simulated broker/account observations after each fill.
            positions={k:p['quantity'] for k,p in state['positions'].items()}
            held=[x for x in items if x.instrument_id in positions and x.ticker!=symbol]
            obs=universe([item]+held,positions)
            return replace(obs,settled=D(state['cash']['settled_usd']),unsettled=D(state['cash']['unsettled_usd']),
                equity=D(state['cash']['liquidation_equity_usd']))
        def refresh_comparative_focus(self,state,symbol,alternatives):
            versions.append(state['version'])
            names={symbol,*alternatives};positions={k:p['quantity'] for k,p in state['positions'].items()}
            selected=[x for x in items if x.ticker in names or x.instrument_id in positions]
            obs=universe(selected,positions)
            return replace(obs,settled=D(state['cash']['settled_usd']),unsettled=D(state['cash']['unsettled_usd']),
                equity=D(state['cash']['liquidation_equity_usd']))
    e.source=Source()
    return e,store,broker,context,versions,args['calendar'].session(NOW.date())


def test_runner_submits_one_bounded_portfolio_call_and_does_not_duplicate_slot(setup_cycle,tmp_path):
    e,_,broker,_,versions,session=engine_fixture(setup_cycle,tmp_path)
    items=[observation(j['instrument_id'],j['symbol']) for j in e.opportunities.jobs.values()]
    e.source.refresh_comparative_focus=lambda state,symbol,alternatives:(versions.append(state['version']) or universe(items))
    executor=ManualExecutor();e.opportunities.executor=executor;seen=[]
    e.brain_factory=lambda:SimpleNamespace(portfolio_decide=lambda context:(seen.append(context) or result()))
    e._run_candidates(session)
    assert len(executor.tasks)==1 and not broker.orders
    context=next(iter(e.opportunities.futures.values()))[2]
    assert len(context.instruments)==6 and context.focal_instrument_id is None
    assert context.comparative_opportunities==()
    executor.tasks[0][0].set_result(executor.tasks[0][1]())
    e._run_candidates(session)
    assert len(executor.tasks)==1 and not broker.orders


def test_research_wiring_is_disabled_by_default_and_enabled_gaps_add_no_paid_calls(setup_cycle,tmp_path):
    e,_,broker,_,versions,session=engine_fixture(setup_cycle,tmp_path)
    assert e.portfolio_research is None
    calls=[]
    def missing_daily(symbols):
        calls.append(symbols);raise ValueError('FIXTURE_DAILY_UNAVAILABLE')
    e.portfolio_research=SessionPortfolioResearch(missing_daily,e.calendar)
    items=[observation(j['instrument_id'],j['symbol']) for j in e.opportunities.jobs.values()]
    e.source.refresh_comparative_focus=lambda state,symbol,alternatives:(versions.append(state['version']) or universe(items))
    executor=ManualExecutor();e.opportunities.executor=executor;brain_calls=[]
    e.brain_factory=lambda:(brain_calls.append('constructed') or SimpleNamespace(portfolio_decide=lambda context:result()))
    e._run_candidates(session)
    assert len(calls)==1 and len(executor.tasks)==1 and brain_calls==['constructed'] and not broker.orders
    context=next(iter(e.opportunities.futures.values()))[2]
    assert all('DAILY_RESEARCH_ERROR:FIXTURE_DAILY_UNAVAILABLE' in item.sector_context
        and 'SEC_USER_AGENT_NOT_CONFIGURED' in item.sector_context for item in context.instruments)


def test_portfolio_call_can_select_one_typed_shortlist_instrument(setup_cycle,tmp_path):
    e,_,broker,_,_,session=engine_fixture(setup_cycle,tmp_path)
    jobs=list(e.opportunities.jobs.values());items=[observation(j['instrument_id'],j['symbol']) for j in jobs]
    context=build_alpha_context(universe(items),e.controller.state,NOW,
        instrument_ids=tuple(j['instrument_id'] for j in jobs))
    anchor,target=jobs[:2];anchor['status']='RESULT_READY'
    e._candidate_result(anchor,context,result(open_raw(NOW,target['instrument_id'],target['symbol'])),session)
    assert len(broker.orders)==1 and target['reason'].startswith('BROKER_')
    assert anchor['reason']=='PORTFOLIO_SELECTED_OTHER'
    e._candidate_result(anchor,context,result(open_raw(NOW,target['instrument_id'],target['symbol'])),session)
    assert len(broker.orders)==1


def test_stale_portfolio_shortlist_never_starts_paid_work(setup_cycle,tmp_path):
    e,_,broker,_,_,session=engine_fixture(setup_cycle,tmp_path)
    items=[replace(observation(j['instrument_id'],j['symbol']),quote_at=NOW-timedelta(seconds=3))
        for j in e.opportunities.jobs.values()]
    e.source.refresh_comparative_focus=lambda *_:universe(items)
    executor=ManualExecutor();e.opportunities.executor=executor
    e.brain_factory=lambda:pytest.fail('stale shortlist must not construct a paid worker')
    e._run_candidates(session)
    assert not executor.tasks and not broker.orders
    assert 'portfolio_entry|'+session.label.isoformat()+'|0' not in e.opportunities.aux


def test_concurrent_positive_results_serialized_and_cannot_exceed_position_limit(setup_cycle,tmp_path):
    e,store,broker,context,versions,session=engine_fixture(setup_cycle,tmp_path)
    jobs=list(e.opportunities.jobs.values())
    # All six model proposals saw the same pre-entry snapshot. Their results
    # are admitted individually against refreshed state, never that snapshot.
    for j in jobs:
        j['status']='RESULT_READY'
        raw=open_raw(NOW,j['instrument_id'],j['symbol'],quantity='1000')
        e._candidate_result(j,context,result(raw),session)
        p=e.controller.state['positions'].get(j['instrument_id'])
        if p is not None:
            # Between runner turns, held-first supervision arms the filled
            # quantity before any next candidate can reach admission.
            e.controller.send(Protection(instrument_id=j['instrument_id'],quantity=p['quantity'],
                stop_usd=p['stop_usd'],evidence_id='held-first-fixture'))
    assert len(e.controller.state['positions'])==2 and len(broker.orders)==2
    assert all(j['status']=='DROPPED' and j['reason']=='SESSION_ENTRY_SUBMISSION_LIMIT' for j in jobs[2:])
    assert versions==sorted(set(versions)) and len(versions)==6
    assert not e.controller.state['risk_breaches']
    # Re-presenting a consumed result must not submit or mint another permit.
    e._candidate_result(jobs[0],context,result(open_raw(NOW,jobs[0]['instrument_id'],jobs[0]['symbol'])),session)
    assert len(broker.orders)==2 and len(e.controller.state['permits'])==6


def test_runner_drains_ready_abstentions_then_stops_after_one_open(setup_cycle,tmp_path):
    e,_,broker,context,_,session=engine_fixture(setup_cycle,tmp_path)
    jobs=list(e.opportunities.jobs.values())[:3]
    values=[result(),result(open_raw(NOW,jobs[1]['instrument_id'],jobs[1]['symbol'])),
        result(open_raw(NOW,jobs[2]['instrument_id'],jobs[2]['symbol']))]
    for n,(job,value) in enumerate(zip(jobs,values)):
        future=Future();future.set_result(value);job.update(status='RUNNING',job_id='ready-'+str(n))
        e.opportunities.futures[job['job_id']]=(future,job['key'],context)
    e._run_candidates(session)
    assert jobs[0]['reason']=='ABSTAIN'
    assert len(broker.orders)==1 and jobs[1]['reason'].startswith('BROKER_')
    assert jobs[2]['status']=='RUNNING' and jobs[2]['job_id'] in e.opportunities.futures
    # The later completed result remains owned by its durable future and is
    # processed on a later writer turn; it is neither lost nor replayed.


def test_oversized_concurrent_proposal_retains_stock_risk_limit(setup_cycle,tmp_path):
    e,_,broker,context,_,session=engine_fixture(setup_cycle,tmp_path)
    a,b=list(e.opportunities.jobs.values())[:2]
    for job in (a,b):
        job['status']='RESULT_READY'
        e._candidate_result(job,context,result(open_raw(NOW,job['instrument_id'],job['symbol'],quantity='5000')),session)
    assert not broker.orders
    assert all(j['status']=='DROPPED' and 'STOCK_EXPOSURE_LIMIT' in j['reason'] for j in (a,b))


def test_daily_downside_rejection_is_a_normal_no_order_disposition(setup_cycle,tmp_path):
    e,_,broker,context,_,session=engine_fixture(setup_cycle,tmp_path)
    job=next(iter(e.opportunities.jobs.values()));job['status']='RESULT_READY'
    e.controller.admit=lambda *args,**kwargs:(_ for _ in ()).throw(Rejected('DAILY_DOWNSIDE_BUDGET'))
    e._candidate_result(job,context,result(open_raw(NOW,job['instrument_id'],job['symbol'])),session)
    assert job['status']=='DROPPED' and job['reason']=='DAILY_DOWNSIDE_BUDGET'
    assert not broker.orders


def test_uncertain_broker_write_never_retried_or_candidate_local(setup_cycle,tmp_path):
    e,_,_,context,_,session=engine_fixture(setup_cycle,tmp_path);job=next(iter(e.opportunities.jobs.values()))
    calls=[]
    def uncertain(*args):calls.append(args);raise ValueError('SUBMISSION_UNCERTAIN_NO_RETRY')
    e.gateway=SimpleNamespace(dispatch=uncertain);job['status']='RESULT_READY'
    ai=result(open_raw(NOW,job['instrument_id'],job['symbol']))
    with pytest.raises(ValueError,match='SUBMISSION_UNCERTAIN_NO_RETRY'):e._candidate_result(job,context,ai,session)
    assert job['reason']=='BROKER_WRITE_UNCERTAIN_NO_REPLAY'
    e._candidate_result(job,context,ai,session)
    assert len(calls)==1 and len(e.controller.state['reservations'])==1


def test_proven_prewrite_stale_rejection_releases_reservation_durably(setup_cycle,tmp_path):
    e,_,_,context,_,session=engine_fixture(setup_cycle,tmp_path);job=next(iter(e.opportunities.jobs.values()))
    class PrewriteGateway:
        submission_attempted=False
        def dispatch(self,*_):raise ValueError('STALE_MARKET_OR_ACCOUNT')
    e.gateway=PrewriteGateway();job['status']='RESULT_READY'
    e._candidate_result(job,context,result(open_raw(NOW,job['instrument_id'],job['symbol'])),session)
    order=next(iter(e.controller.state['orders'].values()))
    assert order['final'] and order['phase']=='REJECTED'
    assert not e.controller.state['reservations'] and not e.controller.state['risk_breaches']
    assert job['status']=='DROPPED' and job['reason']=='STALE_MARKET_OR_ACCOUNT'


def test_closed_session_discards_open_without_account_refresh_or_order(setup_cycle,tmp_path):
    e,_,broker,context,versions,session=engine_fixture(setup_cycle,tmp_path);job=next(iter(e.opportunities.jobs.values()))
    e.clock=lambda:session.closes_at;job['status']='RESULT_READY'
    e._candidate_result(job,context,result(open_raw(NOW,job['instrument_id'],job['symbol'])),session)
    assert job['reason']=='SESSION_CLOSED' and not versions and not broker.orders


def test_shadow_pending_does_not_block_queue_or_held_path(tmp_path):
    store=Store();e=Engine(tmp_path,RuntimeConfig(),store,clock=lambda:NOW)
    e.background['shadow']=(Future(),None,None,NOW)
    e._drain_background()
    q=e._queue();q.refill([observation('small','SMALL')],NOW.date())
    assert q.eligible_jobs() and q.capacity()==2
    assert 'shadow' in e.background


def test_dashboard_pipeline_health_is_compact_and_contains_no_model_payload():
    q,_,_,_=queue_fixture();a=next(iter(q.jobs.values()));q.prepare(a)
    health=q.health()
    assert health['pending']==2 and health['in_flight']==1 and health['last_progress']
    assert health['candidates'][0]['age_seconds']==0
    assert 'fingerprints' not in str(health) and 'context' not in health


def test_persisted_queue_health_reaches_dashboard(setup_cycle):
    from pathlib import Path
    from uuid import uuid4
    from autonomous_trader.monitoring.store import Store as DurableStore
    from autonomous_trader.durable.ledger import LocalDatabase
    from autonomous_trader.monitoring.views import DashboardReader
    args,_,_=setup_cycle;store=DurableStore()
    # The real runner may be heartbeating. This fixture must never write its
    # shared runtime row, nor depend on winning a race against that heartbeat.
    db=LocalDatabase('postgres','m2_admin',schema=args['controller'].ledger.config.schema)
    with db.connect() as conn:
        conn.execute('CREATE TABLE events(id text PRIMARY KEY,at timestamptz,kind text,mode text,payload jsonb)')
        conn.execute('CREATE TABLE runtime(id integer PRIMARY KEY,heartbeat timestamptz,payload jsonb)')
    store.db=db
    mode='PIPELINE_TEST_'+uuid4().hex;q=OpportunityQueue(store,mode,lambda:NOW,executor=ManualExecutor())
    q.refill([observation('small','SMALL')],NOW.date());job=next(iter(q.jobs.values()))
    q.finish(job,'DROPPED','STALE_BARS')
    restored=OpportunityQueue(store,mode,lambda:NOW,executor=ManualExecutor())
    assert restored.jobs[job['key']]['reason']=='STALE_BARS'
    store.heartbeat({'status':'RUNNING','mode':mode,'ledger_schema':None,'account_id':None,'pipeline':q.health()})
    reader=DashboardReader(Path(__file__).parents[2]);reader.db=db
    view=reader.snapshot(NOW)
    assert view['system']['pipeline']['dropped']==1
    assert view['system']['pipeline']['candidates'][0]['symbol']=='SMALL'


def test_expired_job_remains_excluded_while_provider_thread_is_running():
    q,_,_,current=queue_fixture();job=next(iter(q.jobs.values()))
    q.submit(job,None,'old',lambda:None);current['at']+=timedelta(minutes=5);q.completed()
    current['at']+=timedelta(minutes=1)
    assert job not in q.eligible_jobs()
    assert not q.submit(job,None,'new',lambda:None)


def test_failed_read_cannot_starve_an_unprocessed_candidate():
    q,_,_,current=queue_fixture();first,second,third=list(q.jobs.values())
    for job in (first,second):q.prepare(job);q.finish(job,'DROPPED','STALE_BARS')
    current['at']+=timedelta(minutes=1)
    assert q.eligible_jobs()[0] is third


def test_cached_queue_account_poll_is_independent_of_discovery(monkeypatch,tmp_path):
    from test_operational_soak import focus_engine
    e,_,session,_,current=focus_engine(monkeypatch,tmp_path,[False,False])
    e._alpha_step(NOW,session)
    # Isolate the account-poll boundary from the independent discovery timer.
    e.next_discovery=e.next_poll+timedelta(seconds=1)
    calls=[]
    def refresh_account(state):
        calls.append('account')
        state['reconciliation']['valid_until']=(current['at']+timedelta(seconds=10)).isoformat()
        return universe(())
    e.source.refresh_account=refresh_account
    e._run_candidates=lambda session:calls.append('queue')
    current['at']=e.next_poll
    e._alpha_step(current['at'],session)
    assert calls==['account']  # Account authority is restored in its own bounded turn.
    e._alpha_step(current['at'],session)
    assert calls==['account','queue']


def test_focused_model_result_preempts_slow_account_refresh(setup_cycle,tmp_path):
    e,_,_,context,_,session=engine_fixture(setup_cycle,tmp_path)
    job=next(iter(e.opportunities.jobs.values()));future=Future()
    job.update(status='RUNNING',job_id='focused-in-flight',deadline=(NOW+timedelta(seconds=90)).isoformat())
    e.opportunities.futures[job['job_id']]=(future,job['key'],context)
    e.next_discovery=NOW+timedelta(minutes=5);e.next_poll=NOW
    e.source.refresh_account=lambda *_:(_ for _ in ()).throw(AssertionError('slow account read delayed focused result'))

    # While Astra is thinking, the short writer turn does not enter a broad
    # broker read.  Held-position supervision would already have run above it.
    e._alpha_step(NOW,session)
    assert job['status']=='RUNNING'
    assert 'Focused Astra decision in flight' in e.state['activity']

    # The very next turn handles the result before any account refresh.  An
    # OPEN would then perform the existing exact post-model broker refresh.
    future.set_result(result())
    e._alpha_step(NOW+timedelta(seconds=5),session)
    assert job['status']=='COMPLETED' and job['reason']=='ABSTAIN'


def test_writer_polls_in_memory_focused_result_without_more_broker_or_model_calls(setup_cycle,tmp_path):
    e,_,_,context,_,_=engine_fixture(setup_cycle,tmp_path)
    job=next(iter(e.opportunities.jobs.values()));future=Future()
    job.update(status='RUNNING',job_id='focused-poll',deadline=(NOW+timedelta(seconds=90)).isoformat())
    e.opportunities.futures[job['job_id']]=(future,job['key'],context)
    assert e._writer_wait_seconds()==1
    job['status']='QUARANTINED'
    assert e._writer_wait_seconds()==5


def test_inflight_model_does_not_block_free_slot_for_fresh_stream_opportunity(setup_cycle,tmp_path):
    e,_,_,context,_,session=engine_fixture(setup_cycle,tmp_path)
    job=next(iter(e.opportunities.jobs.values()));future=Future()
    job.update(status='RUNNING',job_id='in-flight',deadline=(NOW+timedelta(seconds=90)).isoformat())
    e.opportunities.futures[job['job_id']]=(future,job['key'],context)
    e.live_stream=SimpleNamespace();calls=[]
    e._run_candidates=lambda session,**kwargs:calls.append(kwargs)
    assert e._prioritize_candidate_result(session)
    assert calls==[{'stream_only':True}] and not future.done()


def test_completed_discovery_updates_watch_during_model_without_stale_account_authority(setup_cycle,tmp_path):
    e,store,_,context,_,session=engine_fixture(setup_cycle,tmp_path)
    job=next(iter(e.opportunities.jobs.values()));model=Future()
    job.update(status='RUNNING',job_id='in-flight',deadline=(NOW+timedelta(seconds=90)).isoformat())
    e.opportunities.futures[job['job_id']]=(model,job['key'],context)
    scan=Future();scan.set_result(replace(universe((observation('new-live','NEW'),)),received_at=NOW-timedelta(seconds=90)))
    e.discovery_pending=scan;e.discovery_started_at=NOW-timedelta(seconds=15);e.discovery_binding='old-account-version'
    before=e.controller.state
    e._alpha_step(NOW,session)
    assert e.discovery_pending is None and e.next_discovery==NOW
    assert any(j['symbol']=='NEW' and j['current'] for j in e.opportunities.jobs.values())
    assert e.controller.state==before and not model.done()
    assert any(k=='DISCOVERY_MARKET_ONLY' for k,_,_ in store.events)


def test_held_supervision_precedes_pending_entry_and_shadow(monkeypatch,tmp_path):
    from test_operational_soak import flat_state
    e=Engine(tmp_path,RuntimeConfig(),Store(),clock=lambda:NOW);session=e.calendar.session(NOW.date())
    state=flat_state(session);state['positions']={'held':{'quantity':'7'}};calls=[]
    e.controller=SimpleNamespace(state=state,lease=lambda:None)
    e.source=SimpleNamespace(refresh_held=lambda state:(calls.append('held-read') or object()))
    e.alpha_manager=SimpleNamespace(tick=lambda *args:(calls.append('held-monitor') or {}))
    e.gateway=SimpleNamespace();e.opportunities=SimpleNamespace();e.next_discovery=NOW+timedelta(minutes=5)
    e._drain_background=lambda:calls.append('background-results')
    e._run_candidates=lambda session:calls.append('entry-queue')
    monkeypatch.setattr('autonomous_trader.monitoring.runner.refresh_session',lambda *a,**k:None)
    e._alpha_step(NOW,session)
    assert calls==['held-read','held-monitor','background-results','entry-queue']


def test_bad_quote_timestamp_is_candidate_local_during_discovery():
    from test_alpha_reset import focused_source
    from autonomous_trader.sandbox.http import Operation
    import time
    source=focused_source();source._universe_symbols=('FOCUS','OTHER');source._universe_at=time.monotonic()
    original=source.readers[Operation.SNAPSHOT]
    class Malformed:
        def read(self,*a,**kw):
            value=original.read(*a,**kw)
            for row in value.body:row['quote_time']='not-a-timestamp'
            return value
    source.readers[Operation.SNAPSHOT]=Malformed()
    value=source.refresh({},portfolio=True)
    assert not value.candidates and source.discovery_stats['detailed_rejections']['INSTRUMENT_PAYLOAD_INVALID']==1
