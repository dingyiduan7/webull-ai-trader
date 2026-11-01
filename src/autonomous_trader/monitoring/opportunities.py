"""Bounded model-only work. The runner alone owns reconciliation/admission/execution.

Every claim/fingerprint is persisted before the provider call. An abandoned claim
is quarantined, never replayed: an uncertain model call may already be billable.
Late results cannot regain authority and stuck workers retain their physical slot.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo
import re


HOT_WATCH_SLOTS=2
HOT_WATCH_TTL_SECONDS=600
HOT_REFRESH_SECONDS=15
HOT_SIGNAL_MAX_AGE_SECONDS=120
HOT_RETRY_SECONDS=5
HOT_PROMOTION_MAX_WAIT_SECONDS=60
HOT_REFRESH_MAX_FAILURES=2
HOT_TRACE_KEYS=('exact_refresh_started_at','exact_refresh_completed_at','first_exact_refresh_completed_at',
    'model_started_at','model_handled_at')
HOT_EPOCH_KEYS=('hot_rank','hot_strength','hot_signal_at','hot_promoted_at','hot_until','hot_refresh_failures',
    'last_refresh_error','hot_epoch_id')+HOT_TRACE_KEYS
NY=ZoneInfo('America/New_York')


def code(exc):
    value=str(exc)
    return value if re.fullmatch(r'[A-Z_ 0-9:]{1,120}',value) else 'CANDIDATE_FAILURE_'+type(exc).__name__.upper()


class OpportunityQueue:
    def __init__(self,store,mode,clock,*,workers=2,calls_per_window=3,window=300,executor=None):
        self.store,self.mode,self.clock=store,mode,clock
        self.workers,self.calls_per_window,self.window=workers,calls_per_window,window
        self.executor=executor or ThreadPoolExecutor(max_workers=workers,thread_name_prefix='opportunity-model')
        saved=store.pipeline_state(mode) if hasattr(store,'pipeline_state') else None
        self.jobs=(saved or {}).get('jobs',{});self.calls=(saved or {}).get('calls',[]);self.aux=(saved or {}).get('aux',{})
        self.revision=(saved or {}).get('revision',0)
        self.last_progress=(saved or {}).get('last_progress');self.futures={}
        now=self.clock();sanitized=False
        current_session=str(now.astimezone(NY).date())
        for job in self.jobs.values():
            # Persisted discovery is evidence, never authority for a later
            # session. Fresh discovery must mark a same-session job current
            # before streaming, model work, or execution can use it.
            if job.get('current') and job.get('session')!=current_session:
                job['current']=False;sanitized=True
        active=sorted((j for j in self.jobs.values() if self._hot(j,now)),
            key=lambda j:(j.get('hot_rank',999),j.get('hot_promoted_at','')))
        for job in active[HOT_WATCH_SLOTS:]:
            job['hot_until']=now.isoformat();job.pop('hot_rank',None);sanitized=True
        for job in self.jobs.values():
            # MQTT revisions are process-local; old counters cannot suppress
            # a fresh transport's material events after a runner restart.
            for key in ('stream_signaled_revision','stream_evaluated_revision'):
                if key in job:job.pop(key);sanitized=True
            if job.get('model_started_at') and not self._trace_coherent(job):
                for key in HOT_TRACE_KEYS:job.pop(key,None)
                sanitized=True
            if job.get('hot_until') and not self._hot(job,now):
                self._retire_hot_epoch(job,block=True);sanitized=True
            if job['status'] in ('PREPARING','RUNNING','RESULT_READY','EXECUTING'):
                self.finish(job,'QUARANTINED','ABANDONED_JOB_NO_REPLAY')
        if self.aux.get('last_hot_trace') and not self._trace_coherent(self.aux['last_hot_trace']):
            self.aux.pop('last_hot_trace',None);sanitized=True
        if sanitized:self.save()

    def save(self):
        self.last_progress=self.clock().isoformat()
        self.revision+=1
        self.store.emit('OPPORTUNITY_QUEUE',{'summary':'Portfolio opportunity queue progressed',
            'jobs':self.jobs,'calls':self.calls,'aux':self.aux,'revision':self.revision,'last_progress':self.last_progress},mode=self.mode,at=self.clock())

    def claim_aux(self,kind,signature):
        if self.aux.get(kind,{}).get('signature')==signature:return False
        self.aux[kind]={'signature':signature,'claimed_at':self.clock().isoformat()};self.save();return True

    def claim_once(self,kind,signature):
        """Persist a no-replay reservation before scheduled paid work starts."""
        if kind in self.aux:return False
        self.aux[kind]={'signature':signature,'claimed_at':self.clock().isoformat()};self.save();return True

    def refill(self,items,session_label,priorities=()):
        now=self.clock();ranking={x['instrument_id']:x['rank'] for x in priorities}
        for job in self.jobs.values():job['current']=False
        for offset,item in enumerate(items):
            if not item.entry_eligible:continue
            key=str(session_label)+'|'+item.instrument_id
            job=self.jobs.get(key)
            if job is not None and job['symbol']!=item.ticker:
                self.finish(job,'QUARANTINED','CANDIDATE_IDENTITY_CHANGED');continue
            if job is None:
                job={'key':key,'instrument_id':item.instrument_id,'symbol':item.ticker,
                    'status':'PENDING','reason':'DISCOVERED','first_at':now.isoformat(),'updated_at':now.isoformat(),
                    'not_before':now.isoformat(),'fingerprints':[],'attempts':0,'session':str(session_label)}
                self.jobs[key]=job
            job['current']=True;job['rank']=ranking.get(item.instrument_id,100+offset)
        # Keep all live jobs and a bounded tail of dispositions; history remains append-only in events.
        if len(self.jobs)>90:
            for key,job in sorted(self.jobs.items(),key=lambda p:p[1]['updated_at']):
                if len(self.jobs)<=90:break
                if not job['current'] and job['status'] not in ('RUNNING','RESULT_READY','EXECUTING'):del self.jobs[key]
        self.save()

    def eligible_jobs(self,held=()):
        now=self.clock()
        running={key for _,key,_ in self.futures.values()}
        return sorted((j for j in self.jobs.values() if j.get('current') and j['instrument_id'] not in held
            and j['key'] not in running
            and not j.get('hot_epoch_blocked_signal_at')
            and j['status'] not in ('PREPARING','RUNNING','RESULT_READY','EXECUTING')
            and now>=datetime.fromisoformat(j['not_before'])),key=lambda j:(
                -1 if j.get('reason')=='STREAM_MATERIAL_CHANGE' else
                0 if self._hot(j,now) and not j.get('hot_refresh_failures') else
                    1 if self._hot(j,now) else 2,
                j.get('hot_rank',999),
                0 if j.get('preferred_until') and now<datetime.fromisoformat(j['preferred_until']) else 1,
                j['attempts'],j.get('rank',999),j['key']))

    def expire_overdue_hot(self):
        now=self.clock()
        for job in self.jobs.values():
            if not self._hot(job,now) or not job.get('hot_promoted_at'):continue
            if job['status'] in ('RUNNING','RESULT_READY','EXECUTING'):continue
            if now-datetime.fromisoformat(job['hot_promoted_at'])>=timedelta(seconds=HOT_PROMOTION_MAX_WAIT_SECONDS):
                if job.get('model_started_at'):
                    # A completed model call already had prompt attention;
                    # retire only its hot membership, not its disposition.
                    self._retire_hot_epoch(job,block=True);self.save()
                else:self.expire_hot(job,'HOT_PROMOTION_EXPIRED')

    def _archive_hot_trace(self,job):
        trace={k:job.get(k) for k in ('hot_epoch_id','hot_signal_at','hot_promoted_at')+HOT_TRACE_KEYS}
        if not self._trace_coherent(trace):return
        old=self.aux.get('last_hot_trace',{})
        if not old.get('model_started_at') or trace['model_started_at']>old['model_started_at']:
            self.aux['last_hot_trace']=trace

    def _retire_hot_epoch(self,job,*,block):
        self._archive_hot_trace(job)
        blocked_signal=job.get('hot_signal_at')
        for key in HOT_EPOCH_KEYS:job.pop(key,None)
        if blocked_signal:
            job['hot_last_signal_at']=blocked_signal
            if block:job['hot_epoch_blocked_signal_at']=blocked_signal

    def expire_hot(self,job,reason):
        now=self.clock()
        job.update(status='DROPPED',reason=reason,updated_at=now.isoformat(),
            not_before=(now+timedelta(seconds=self.window)).isoformat())
        self._retire_hot_epoch(job,block=True)
        self.save()

    def hot_retry_wait_until(self,held=()):
        now=self.clock()
        waits=[datetime.fromisoformat(j['not_before']) for j in self.jobs.values()
            if j.get('current') and j['instrument_id'] not in held and self._hot(j,now)
            and j['status']=='PENDING' and j.get('reason')=='HOT_EXACT_REFRESH_RETRY'
            and datetime.fromisoformat(j['not_before'])>now]
        return min(waits) if waits else None

    def hot_retry_pending(self,held=()):
        now=self.clock()
        return any(j.get('current') and j['instrument_id'] not in held and self._hot(j,now)
            and j['status']=='PENDING' and j.get('reason')=='HOT_EXACT_REFRESH_RETRY'
            for j in self.jobs.values())

    def retry_hot_refresh(self,job,reason):
        now=self.clock();failures=job.get('hot_refresh_failures',0)+1
        job['hot_refresh_failures']=failures
        if (failures>=HOT_REFRESH_MAX_FAILURES or not self._hot(job,now)
                or now-datetime.fromisoformat(job['hot_promoted_at'])>=timedelta(seconds=HOT_PROMOTION_MAX_WAIT_SECONDS)):
            self.expire_hot(job,'HOT_EXACT_REFRESH_EXPIRED');return False
        job.update(status='PENDING',reason='HOT_EXACT_REFRESH_RETRY',
            last_refresh_error=reason,updated_at=now.isoformat(),
            not_before=(now+timedelta(seconds=HOT_RETRY_SECONDS)).isoformat())
        self.save();return True

    @staticmethod
    def _hot(job,now):
        try:return bool(job.get('hot_until')) and now<datetime.fromisoformat(job['hot_until'])
        except (TypeError,ValueError):return False

    @staticmethod
    def _trace_coherent(trace):
        try:
            signal=datetime.fromisoformat(trace['hot_signal_at']);promoted=datetime.fromisoformat(trace['hot_promoted_at'])
            model=datetime.fromisoformat(trace['model_started_at'])
            if not signal<=promoted<=model:return False
            if trace.get('hot_until') and model>=datetime.fromisoformat(trace['hot_until']):return False
            if trace.get('exact_refresh_started_at') and not promoted<=datetime.fromisoformat(trace['exact_refresh_started_at'])<=model:return False
            if trace.get('exact_refresh_completed_at') and not promoted<=datetime.fromisoformat(trace['exact_refresh_completed_at'])<=model:return False
            if trace.get('first_exact_refresh_completed_at') and not promoted<=datetime.fromisoformat(trace['first_exact_refresh_completed_at'])<=model:return False
            if trace.get('model_handled_at') and not model<=datetime.fromisoformat(trace['model_handled_at']):return False
            return True
        except (KeyError,TypeError,ValueError):return False

    def promote_hot(self,rows,*,slots=HOT_WATCH_SLOTS,ttl=HOT_WATCH_TTL_SECONDS):
        """Promote bounded broad-screen evidence; this grants no order authority."""
        now=self.clock();promoted=[];ranked=[]
        for row in rows or ():
            try:
                signal_at=datetime.fromisoformat(row['screen_at']);strength=Decimal(row['percent_per_minute'])
                age=(now-signal_at).total_seconds()
                if not 0<=age<=HOT_SIGNAL_MAX_AGE_SECONDS or not strength.is_finite():continue
                ranked.append((strength,row,signal_at))
            except (KeyError,TypeError,ValueError,InvalidOperation):continue
        ranked.sort(key=lambda x:x[0],reverse=True);selected=[];selected_keys=set()
        for strength,row,signal_at in ranked:
            if len(selected)>=slots:break
            matches=[j for j in self.jobs.values() if j.get('current') and j.get('symbol')==row.get('symbol')]
            if len(matches)!=1:continue
            job=matches[0]
            blocked=job.get('hot_epoch_blocked_signal_at')
            if blocked and signal_at<=datetime.fromisoformat(blocked):continue
            last_signal=job.get('hot_last_signal_at')
            if (last_signal and signal_at<=datetime.fromisoformat(last_signal)
                    and not (self._hot(job,now) and job.get('hot_signal_at')==signal_at.isoformat())):continue
            if job['key'] in selected_keys:continue
            selected_keys.add(job['key']);selected.append((len(selected)+1,strength,row,signal_at,job))
        for job in self.jobs.values():
            if job['key'] not in selected_keys and self._hot(job,now):
                self._retire_hot_epoch(job,block=False)
        for rank,strength,row,signal_at,job in selected:
            if job['status'] in ('PREPARING','RUNNING','RESULT_READY','EXECUTING'):
                job['hot_rank']=rank
                continue
            if self._hot(job,now) and job.get('hot_signal_at')==signal_at.isoformat():
                job['hot_rank']=rank
                continue
            previous=job.get('hot_signal_at') or job.get('hot_epoch_blocked_signal_at');old_strength=job.get('hot_strength')
            materially_renewed=previous is None or bool(job.get('hot_epoch_blocked_signal_at'))
            if previous!=signal_at.isoformat() and old_strength is not None:
                old=Decimal(old_strength)
                materially_renewed=abs(strength-old)>=max(Decimal('.5'),abs(old)*Decimal('.25'))
            self._archive_hot_trace(job)
            for key in HOT_TRACE_KEYS:job.pop(key,None)
            job.pop('hot_epoch_blocked_signal_at',None)
            job.update(hot_rank=rank,hot_strength=str(strength),hot_signal_at=signal_at.isoformat(),
                hot_last_signal_at=signal_at.isoformat(),
                hot_promoted_at=now.isoformat(),hot_until=(now+timedelta(seconds=ttl)).isoformat(),
                hot_refresh_failures=0,hot_epoch_id=uuid4().hex)
            if materially_renewed and job['status'] not in ('PREPARING','RUNNING','RESULT_READY','EXECUTING'):
                job['not_before']=now.isoformat();job['reason']='HOT_SIGNAL_RENEWED'
            promoted.append({'symbol':job['symbol'],'instrument_id':job['instrument_id'],'rank':rank,
                'hot_epoch_id':job['hot_epoch_id'],
                'signal_at':signal_at.isoformat(),'promoted_at':now.isoformat(),'materially_renewed':materially_renewed})
        if promoted:self.save()
        return promoted

    def prefer(self,instrument_id,symbol,held=()):
        matches=[j for j in self.jobs.values() if j.get('current') and j['instrument_id']==instrument_id
            and j['symbol']==symbol and instrument_id not in held]
        if len(matches)!=1:return False
        job=matches[0]
        if job['status'] in ('PREPARING','RUNNING','RESULT_READY','EXECUTING'):return False
        # Change ordering only. Never reset cooldown, fingerprints or model budget.
        job['preferred_until']=(self.clock()+timedelta(seconds=self.window)).isoformat();self.save();return True

    def capacity(self):
        now=self.clock();self.calls=[t for t in self.calls if now-datetime.fromisoformat(t)<timedelta(seconds=self.window)]
        return min(self.workers-len(self.futures),self.calls_per_window-len(self.calls))

    def prepare(self,job):
        job.pop('preferred_until',None)
        job.update(status='PREPARING',reason='REFRESHING',attempts=job['attempts']+1,updated_at=self.clock().isoformat());self.save()

    def record_observation(self,job,observation,at,*,regime_label=None,screen_evidence=None):
        """Keep a compact durable session dossier; it grants no order authority."""
        bars=sorted(observation.bars,key=lambda row:row['time'])
        first=Decimal(str(bars[0]['close'])) if bars else None
        last=Decimal(str(bars[-1]['close'])) if bars else None
        row={'at':at.isoformat(),'quote_at':observation.quote_at.isoformat(),'bid':str(observation.bid),
            'ask':str(observation.ask),'bid_size':None if observation.bid_size is None else str(observation.bid_size),
            'ask_size':None if observation.ask_size is None else str(observation.ask_size),
            'volume':None if observation.volume is None else str(observation.volume),
            'relative_volume_10d':None if observation.relative_volume_10d is None else str(observation.relative_volume_10d),
            'short_window_change_ratio':None if observation.short_window_change_ratio is None else str(observation.short_window_change_ratio),
            'short_window_minutes':observation.short_window_minutes,
            'bar_first_at':bars[0]['time'] if bars else None,'bar_last_at':bars[-1]['time'] if bars else None,
            'bar_return_bps':None if first is None or not first else str((last-first)/first*10000),
            'regime_label':regime_label,'screen_evidence':screen_evidence}
        dossier=job.setdefault('dossier',{'quotes':[],'decisions':[]})
        dossier['quotes']=(dossier.get('quotes',[])+[row])[-24:]
        for decision in dossier.get('decisions',[]):
            if not decision.get('quote_at'):continue
            if (observation.quote_at<=datetime.fromisoformat(decision['quote_at'])
                    or at<=datetime.fromisoformat(decision['at'])):continue
            if decision['action']!='ABSTAIN' and not decision.get('execution_block'):continue
            ask=Decimal(decision['ask']) if decision.get('ask') else None
            if not ask:continue
            change=(observation.bid-ask)/ask*10000
            decision['best_bps']=str(max(change,Decimal(decision.get('best_bps',str(change)))))
            decision['worst_bps']=str(min(change,Decimal(decision.get('worst_bps',str(change)))))
            if decision.get('last_outcome_bid')==str(observation.bid):continue
            decision['last_outcome_bid']=str(observation.bid)
            self.store.emit('OPPORTUNITY_OUTCOME',{'summary':'Observed unfilled opportunity path; not realized profit',
                'decision_id':decision.get('decision_id',job['key']+'|'+decision['at']),
                'symbol':job['symbol'],'strategy':decision.get('strategy'),'thesis':decision.get('reason'),
                'market_context':decision.get('market_context'),'action':decision['action'],
                'execution_block':decision.get('execution_block'),
                'failure_origin':('ASTRA_DECISION' if decision['action']=='ABSTAIN' else
                    ('INFRASTRUCTURE' if decision.get('execution_block') in {'ALPHA_MARKET_CONTEXT_TOO_LARGE',
                        'STALE_MARKET_OR_ACCOUNT','STALE_MARKET','BROKER_WRITE_UNCERTAIN_NO_REPLAY'} else 'EXECUTION_CONDITION')),
                'started_at':decision['at'],'evaluated_at':row['at'],'entry_reference_ask':str(ask),
                'later_bid':str(observation.bid),'mfe_bps':decision['best_bps'],'mae_bps':decision['worst_bps'],
                'holding_seconds':None,'realized_net_pnl_usd':None,'filled':False,
                'model_cost_usd':decision.get('model_cost_usd'),
                'sample_policy':'EXPLORATORY_COUNTERFACTUAL_NO_ASSUMED_FILL_OR_NET_PROFIT'},mode=self.mode,at=at)
        self.save()

    def record_decision(self,job,action,reason,at,*,details=None):
        job.pop('execution_feedback',None)  # The new model decision consumed this feedback.
        dossier=job.setdefault('dossier',{'quotes':[],'decisions':[]});quote=(dossier.get('quotes') or [None])[-1]
        dossier['decisions']=(dossier.get('decisions',[])+[{'at':at.isoformat(),'action':action,
            'reason':str(reason)[:900],'ask':quote.get('ask') if quote else None,
            'quote_at':quote.get('quote_at') if quote else None,**(details or {})}])[-12:]
        self.save()

    def dossier(self,job,now):
        """Decision evidence only. Later bid marks use executable-side returns, never hindsight highs."""
        saved=job.get('dossier') or {};quotes=saved.get('quotes',[])[-24:];decisions=[]
        for decision in saved.get('decisions',[])[-12:]:
            later=[q for q in quotes if q.get('at','')>decision.get('at','')]
            ask=Decimal(str(decision['ask'])) if decision.get('ask') is not None else None
            bids=[Decimal(str(q['bid'])) for q in later if q.get('bid') is not None]
            outcomes={'later_observations':len(bids),'best_executable_return_bps':None,'worst_executable_return_bps':None}
            if ask and bids:
                outcomes.update(best_executable_return_bps=str((max(bids)-ask)/ask*10000),
                    worst_executable_return_bps=str((min(bids)-ask)/ask*10000))
            decisions.append({**decision,**outcomes})
        latest_screen=(quotes[-1].get('screen_evidence') if quotes else None)
        return {'scope':'SESSION_OPPORTUNITY_EVIDENCE_NO_EXECUTION_AUTHORITY','symbol':job['symbol'],
            'instrument_id':job['instrument_id'],'as_of':now.isoformat(),'attempts':job.get('attempts',0),
            'first_detected_at':job.get('first_at'),'latest_screen_evidence':latest_screen,
            'current_status':job.get('status'),'current_reason':job.get('reason'),'quote_sequence':quotes,
            'prior_astra_decisions':decisions,
            'execution_feedback':job.get('execution_feedback'),
            'interpretation':'Displayed top-of-book is instantaneous, not total executable liquidity. Returns use later bid versus prior ask, exclude unmeasured slippage and fill probability, and are exploratory.'}

    def submit(self,job,context,signature,work,*,unchanged_cooldown=None):
        if job['status'] in ('RUNNING','RESULT_READY','EXECUTING') or any(key==job['key'] for _,key,_ in self.futures.values()):
            return False
        if signature in job['fingerprints']:
            self.finish(job,'COMPLETED','UNCHANGED_CONTEXT',cooldown=self.window if unchanged_cooldown is None else unchanged_cooldown);return False
        if self.capacity()<=0:
            self.finish(job,'PENDING','MODEL_BUDGET_WAIT',cooldown=5);return False
        now=self.clock();job_id=uuid4().hex
        attempts=job['attempts'] if job['status']=='PREPARING' else job['attempts']+1
        job.update(status='RUNNING',reason='MODEL_IN_FLIGHT',job_id=job_id,updated_at=now.isoformat(),
            deadline=(now+timedelta(seconds=90)).isoformat(),model_started_at=now.isoformat(),attempts=attempts)
        # Keep a full-session working set so cycling through a small set of
        # unchanged states cannot purchase duplicate calls.
        job['fingerprints']=(job['fingerprints']+[signature])[-256:];self.calls.append(now.isoformat())
        self.save()  # Durable claim BEFORE submitting model work.
        try:self.futures[job_id]=(self.executor.submit(work),job['key'],context)
        except Exception:
            self.finish(job,'QUARANTINED','WORKER_SUBMIT_FAILED');raise
        return True

    def completed(self,limit=None):
        results=[];now=self.clock()
        for job_id,(future,key,context) in list(self.futures.items()):
            if limit is not None and len(results)>=limit:break
            job=self.jobs[key]
            if future.done():
                del self.futures[job_id]
                if job['status']!='RUNNING':
                    # Still record a returned billable result, but never execute it.
                    try:value=future.result()
                    except Exception:value=None
                    if value is not None:self.store.emit('ALPHA_DECISION',{'summary':'Late candidate result discarded',
                        'ai':value,'call_type':'PORTFOLIO_ENTRY','job_id':job_id,'disposition':'LATE_RESULT_NO_AUTHORITY'},mode=self.mode,at=now)
                    continue
                try:value=future.result()
                except Exception as exc:
                    self.finish(job,'DROPPED',code(exc));continue
                expires_at=getattr(context,'expires_at',None)
                deadline=job.get('deadline')
                if (deadline is not None and now>=datetime.fromisoformat(deadline)) or (expires_at is not None and now>=expires_at):
                    self.store.emit('ALPHA_DECISION',{'summary':'Late candidate result discarded',
                        'ai':value,'call_type':'PORTFOLIO_ENTRY','job_id':job_id,'disposition':'LATE_RESULT_NO_AUTHORITY'},mode=self.mode,at=now)
                    self.finish(job,'QUARANTINED','MODEL_RESULT_STALE_NO_AUTHORITY');continue
                job.update(status='RESULT_READY',reason='MODEL_COMPLETED',updated_at=now.isoformat());self.save()
                results.append((job,context,value))
            elif job['status']=='RUNNING' and now>=datetime.fromisoformat(job['deadline']):
                self.finish(job,'QUARANTINED','MODEL_DEADLINE_NO_REPLAY')
        return results

    def finish(self,job,status,reason,*,cooldown=30):
        now=self.clock();jitter=int(sha256(job['key'].encode()).hexdigest()[:2],16)%16
        job.update(status=status,reason=reason,updated_at=now.isoformat(),not_before=(now+timedelta(seconds=cooldown+jitter)).isoformat())
        self.save()

    def health(self):
        now=self.clock();counts={k:0 for k in ('pending','in_flight','completed','dropped')};rows=[]
        for j in self.jobs.values():
            group=('in_flight' if j['status'] in ('PREPARING','RUNNING','RESULT_READY','EXECUTING') else
                'pending' if j['status']=='PENDING' else 'completed' if j['status']=='COMPLETED' else 'dropped')
            counts[group]+=1
            rows.append({k:j.get(k) for k in ('symbol','instrument_id','status','reason','updated_at','not_before','attempts','hot_rank','hot_until')}
                |{'age_seconds':max(0,int((now-datetime.fromisoformat(j['first_at'])).total_seconds()))})
        active_hot=sorted((j for j in self.jobs.values() if self._hot(j,now)),key=lambda j:j.get('hot_rank',999))
        focus_traces=[j for j in self.jobs.values() if j.get('hot_promoted_at') and j.get('first_exact_refresh_completed_at')]
        if (self._trace_coherent(self.aux.get('last_hot_trace',{}))
                and self.aux['last_hot_trace'].get('first_exact_refresh_completed_at')):
            focus_traces.append(self.aux['last_hot_trace'])
        first_focus=None;first_focus_at=None
        if focus_traces:
            recent_focus=max(focus_traces,key=lambda j:j['first_exact_refresh_completed_at'])
            try:
                duration=(datetime.fromisoformat(recent_focus['first_exact_refresh_completed_at'])-
                    datetime.fromisoformat(recent_focus['hot_promoted_at'])).total_seconds()
                if duration>=0:
                    first_focus=round(duration,3)
                    first_focus_at=recent_focus['first_exact_refresh_completed_at']
            except ValueError:pass
        traced=[j for j in self.jobs.values() if j.get('model_started_at') and self._trace_coherent(j)]
        if self._trace_coherent(self.aux.get('last_hot_trace',{})):traced.append(self.aux['last_hot_trace'])
        latest=max(traced,key=lambda j:j['model_started_at']) if traced else None
        signal_to_model=(datetime.fromisoformat(latest['model_started_at'])-datetime.fromisoformat(latest['hot_signal_at'])).total_seconds() if latest else None
        promotion_to_model=(datetime.fromisoformat(latest['model_started_at'])-datetime.fromisoformat(latest['hot_promoted_at'])).total_seconds() if latest and latest.get('hot_promoted_at') else None
        promotion_to_handled=(datetime.fromisoformat(latest['model_handled_at'])-datetime.fromisoformat(latest['hot_promoted_at'])).total_seconds() if latest and latest.get('model_handled_at') and latest.get('hot_promoted_at') else None
        return {'last_progress':self.last_progress,**counts,'workers':self.workers,'occupied_workers':len(self.futures),
            'worker_health':'DEGRADED' if any(self.jobs[k]['status']=='QUARANTINED' for _,k,_ in self.futures.values()) else 'OK',
            'calls_in_window':len(self.calls),'call_limit':self.calls_per_window,'window_seconds':self.window,
            'hot_watch':{'slots':HOT_WATCH_SLOTS,'refresh_seconds':HOT_REFRESH_SECONDS,'ttl_seconds':HOT_WATCH_TTL_SECONDS,
                'target_model_start_seconds':'15-30','symbols':[j['symbol'] for j in active_hot],
                'last_promotion_to_first_focus_seconds':first_focus,'last_first_focus_at':first_focus_at,
                'last_model_started_at':latest['model_started_at'] if latest else None,
                'last_signal_to_model_start_seconds':round(signal_to_model,3) if signal_to_model is not None else None,
                'last_promotion_to_model_start_seconds':round(promotion_to_model,3) if promotion_to_model is not None else None,
                'last_promotion_to_verdict_seconds':round(promotion_to_handled,3) if promotion_to_handled is not None else None},
            'candidates':rows}
