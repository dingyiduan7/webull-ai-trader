"""Single-writer REAL cash portfolio loop. Startup is gated; no scheduler here."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4
from hashlib import sha256
import json
import re

from .recovery_reasons import FLAT_RECOVERABLE_REASONS,HELD_RECOVERABLE_REASONS
from .contracts import AlphaManagedPlan,AlphaPositionContext,AlphaDecisionEnvelope,OrderIntent,payload_hash
from .decisions import parse_alpha_decision,validate_alpha_semantics
from .mvp.alpha import translate_open
from .monitoring.decision_evidence import build_portfolio_decision_evidence,persist_portfolio_decision_evidence
from .durable.models import Protection
from .reference import Session
from .real_trading import CAPABILITY_VERSION,SOURCE,flat_recovery_values,confirm_flat_settlements
from .real_waiting import market_unavailable,clean_flat,recoverable_flat_incident,service_receipt,resolution_baseline
from .mvp.config import reviewed_calendar
from .reference import NEW_YORK
from psycopg import errors as psycopg_errors
from .real_read_budget import ReadRateLimited
from .carry import plan_carries,carry_enabled,clean_carry,carry_account_values,snap_exit
from .risk import POLICY

BOUNDARIES='''REAL individual CASH. Maximize realized net profit after broker fees, spread/slippage and model costs.
Deployment principal <= $500; per-symbol exposure <= 40% of current reconciled liquidation equity.
Existing account risk limits remain immutable. No deterministic trade-count ceiling.
Capacity is reserved for an entry-to-flat position round trip; add-ons share its ticket.
Zero-fill final orders release it and protective exits never consume capacity.
No leverage, shorting or money movement.
Whole-share LIMIT entries; exact owned-share exits. Holding period is the model's choice (minutes to days) within the
supplied exit horizon; deterministic hard-stop and deadline exits.
Evaluate the supplied portfolio together; do not fan out paid candidate calls. Quality and positive expected net payoff
must justify capital, risk and all costs. Missing research is unknown, never favorable evidence.
Preserve a still-valid thesis through ordinary price noise; exit on real invalidation or changed expected payoff.
Stops are local and monitored during the regular session only: an overnight gap can fill below a stop.'''

REAL_WRITER_LOCK=17482026
POST_CLOSE_ATTEMPTS=10
POST_CLOSE_RETRY_WINDOW=timedelta(minutes=90)
SAFETY_CANCEL_WAIT_CYCLES=10


def portfolio_context_key(account_id,session,packet,trigger=None):
    # Idempotency key for one entry call. Price evidence is NOT hashed here:
    # whether prices moved enough to justify a call is decided by
    # real_triggers.entry_trigger with hysteresis, and its reason/time is the
    # trigger token. Quote ticks therefore never change this key.
    material={'instruments':sorted(q.instrument_id for q in packet.context.instruments),
        'execution_cost_evidence':packet.context.execution_cost_evidence,'trigger':trigger}
    signature=sha256(json.dumps(material,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return 'real-portfolio-context-'+account_id+'-'+session.label.isoformat()+'-'+signature


# No latch sells a holding (owner decisions 2026-10-15/16: "never force sell ... don't panic sell just because we
# are losing"). Every latch -- daily loss, drawdown, termination, software incident -- freezes new entries and
# asks the decision model to review each holding; positions stay under their own stops and deadlines, which the
# model chose at entry as its invalidation and selling point.
LIQUIDATING_LATCHES=()


def _reconciliation_failure_subcode(exc,stage):
    """Retain a bounded failure category, never arbitrary provider text."""
    raw=str(exc)
    if isinstance(exc,ValueError) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}',raw):return raw
    kinds={ValueError:'VALUE_ERROR',KeyError:'KEY_ERROR',TypeError:'TYPE_ERROR',
        TimeoutError:'TIMEOUT_ERROR',ConnectionError:'CONNECTION_ERROR',OSError:'OS_ERROR'}
    kind=next((label for cls,label in kinds.items() if isinstance(exc,cls)),'UNEXPECTED_ERROR')
    return stage+'_'+kind


class RealRunner:
    def __init__(self,controller,source,gateway,gate,store,brain_factory,clock=lambda:datetime.now(timezone.utc)):
        self.controller,self.source,self.gateway,self.gate,self.store=controller,source,gateway,gate,store
        self.brain_factory,self.clock=brain_factory,clock
        self.worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix='real-portfolio')
        self.pending=None
        self.position_call_blocked_until={}
        self.reconciliation_successes=[]

    def emit(self,kind,payload,*,key=None):
        self.store.emit_idempotent(kind,payload,mode='REAL',at=self.clock(),event_id=key or uuid4().hex)

    def _incident(self,reason,**evidence):
        before=self.controller.state.get('incidents',{})
        if not any(row.get('reason')==reason for row in before.values()):
            before_ids=set(before);self.controller.incident(reason)
            created=tuple(sorted(set(self.controller.state.get('incidents',{}))-before_ids))
            incident_id=created[0] if len(created)==1 else uuid4().hex
            self.emit('REAL_RECOVERY_INCIDENT',{'reason':reason,'incident_id':incident_id,**evidence},
                key='real-recovery-'+incident_id)
        return reason

    def _reset_reconciliation_successes(self):
        self.reconciliation_successes=[]

    def _single_writer(self):
        with self.store.db.connect() as connection:
            rows=connection.execute("SELECT granted,count(*) FROM pg_locks "
                "WHERE locktype='advisory' AND objid=%s GROUP BY granted",(REAL_WRITER_LOCK,)).fetchall()
        counts={bool(granted):int(count) for granted,count in rows}
        return counts.get(True,0)==1 and counts.get(False,0)==0

    def _eligible_held_incidents(self,packet):
        """Operational read failures while holding: broker and ledger agree exactly on positions and cash."""
        state=self.controller.state;incidents=state.get('incidents',{})
        ids=tuple(sorted(key for key,value in incidents.items()
            if value.get('reason') in HELD_RECOVERABLE_REASONS and value.get('pending_correction') is False))
        if not ids or set(ids)!=set(incidents) or not state['positions']:return ()
        cash=state['cash']
        ledger={iid:Decimal(str(row['quantity'])) for iid,row in state['positions'].items() if Decimal(str(row['quantity']))!=0}
        broker={str(k):Decimal(str(v)) for k,v in dict(packet.positions).items() if Decimal(str(v))!=0}
        if (state['reservations'] or packet.open_client_ids or broker!=ledger
                or any(not row['final'] or row.get('phase')=='UNKNOWN' for row in state['orders'].values())
                or any(state['latches'].get(name,False) for name in ('daily','drawdown','terminated'))
                or not state['latches'].get('incident',False)
                or not state['reconciliation']['ready'] or not state['reconciliation']['settlement_known']
                or packet.settled!=Decimal(cash['settled_usd'])
                or packet.unsettled!=Decimal(cash['unsettled_usd'])):return ()
        return ids

    def _eligible_reconciliation_incidents(self,packet,checkpoint):
        state=self.controller.state;incidents=state.get('incidents',{})
        if state['positions']:return self._eligible_held_incidents(packet)
        ids=tuple(sorted(key for key,value in incidents.items()
            if value.get('reason') in FLAT_RECOVERABLE_REASONS
            and value.get('pending_correction') is False))
        if not ids or set(ids)!=set(incidents):return ()
        cash=state['cash'];baseline_cash=resolution_baseline(state,checkpoint if isinstance(checkpoint,dict) else {})
        if baseline_cash is None:return ()
        if (state['positions'] or state['reservations'] or packet.positions or packet.open_client_ids
                or any(not row['final'] or row.get('phase')=='UNKNOWN' for row in state['orders'].values())
                or any(state['latches'].get(name,False) for name in ('daily','drawdown','terminated'))
                or not state['latches'].get('incident',False)
                or not state['reconciliation']['ready'] or not state['reconciliation']['settlement_known']
                or packet.settled!=Decimal(cash['settled_usd'])
                or packet.unsettled!=Decimal(cash['unsettled_usd'])
                or packet.equity!=Decimal(cash['liquidation_equity_usd'])
                or (packet.settled,packet.unsettled,packet.equity)!=baseline_cash):return ()
        return ids

    def _record_reconciliation_success(self,packet,checkpoint):
        ids=self._eligible_reconciliation_incidents(packet,checkpoint)
        if not ids:self._reset_reconciliation_successes();return False
        observation={'evidence_id':packet.evidence_id,'observed_at':packet.observed_at.isoformat()}
        if self.reconciliation_successes:
            prior=self.reconciliation_successes[-1]
            if (prior['evidence_id']==observation['evidence_id']
                    or datetime.fromisoformat(prior['observed_at'])>=packet.observed_at):
                self._reset_reconciliation_successes();return False
        self.reconciliation_successes.append(observation)
        if len(self.reconciliation_successes)<2:return False
        self.reconciliation_successes=self.reconciliation_successes[-2:]
        if not self._single_writer():self._reset_reconciliation_successes();return False
        receipt=getattr(self,'authority_receipt',{})
        if (receipt.get('account_binding')!=packet.account_binding
                or receipt.get('market_session_label')!=packet.context.session_open.astimezone(NEW_YORK).date().isoformat()):
            self._reset_reconciliation_successes();return False
        held=len(self.controller.state['positions'])
        evidence={'incident_ids':list(ids),'observations':list(self.reconciliation_successes),
            'account_binding':packet.account_binding,'build_sha256':receipt.get('build_sha256'),
            'market_session_label':receipt.get('market_session_label'),'broker_positions':len(packet.positions),
            'broker_open_orders':0,'local_positions':held,'local_nonfinal_orders':0,
            'settled_usd':str(packet.settled),'unsettled_usd':str(packet.unsettled),
            'equity_usd':str(packet.equity),'settlement_known':True,'broker_mutations':0,'writer_count':1}
        self.emit('REAL_RECONCILIATION_RESOLUTION_EVIDENCE',evidence,
            key='real-reconciliation-resolution-'+sha256(json.dumps(
                evidence,sort_keys=True,separators=(',',':')).encode()).hexdigest())
        if held:self.controller.resolve_held_reconciliation_incidents(packet,ids)
        else:self.controller.resolve_flat_reconciliation_incidents(packet,ids)
        self._reset_reconciliation_successes();return True

    def _reconciliation_failure(self,exc,stage):
        self._reset_reconciliation_successes()
        return self._incident('REAL_ACCOUNT_RECONCILIATION_FAILED',failure_stage=stage,
            failure_subcode=_reconciliation_failure_subcode(exc,stage))

    def last(self,kind,plan_id=None):
        with self.store.db.connect() as c:
            row=c.execute("SELECT payload FROM events WHERE mode='REAL' AND kind=%s "
                "AND (%s::text IS NULL OR payload->>'plan_id'=%s) ORDER BY at DESC,id DESC LIMIT 1",
                (kind,plan_id,plan_id)).fetchone()
        return row[0] if row else None

    def plan(self,plan_id):
        value=self.last('REAL_PLAN',plan_id)
        if not value:raise ValueError('REAL_POSITION_THESIS_MISSING')
        return AlphaManagedPlan.model_validate(value['plan'])

    def context(self,packet):
        state=self.controller.state;positions=[]
        for iid,p in state['positions'].items():
            oid=p['originating_orders'][0];plan=self.plan(state['orders'][oid]['intent']['plan_id'])
            quantity=Decimal(p['quantity']);basis=Decimal(p['basis_usd']);mark=Decimal(p['mark_usd'])
            session=reviewed_calendar().session(self.clock().astimezone(NEW_YORK).date())
            carried=bool(session and plan.latest_exit_at<session.opens_at)
            positions.append(AlphaPositionContext(position_id='position-'+iid,instrument_id=iid,symbol=plan.symbol,
                quantity=quantity,average_entry_usd=basis/quantity,mark_usd=mark,active_stop_usd=p['stop_usd'],
                latest_exit_at=session.liquidation_at if carried else plan.latest_exit_at,
                unrealized_pnl_usd=quantity*mark-basis,target_usd=plan.target_usd,
                original_thesis=plan.thesis,strategy_label=plan.strategy_label,entry_limit_usd=plan.entry_limit_usd,
                original_stop_usd=plan.original_stop_usd,
                # A position held overnight is reviewed from the first quote of the new session.
                next_review_at=self.clock() if carried or (session and plan.next_review_at<session.opens_at) else plan.next_review_at,
                plan_version=plan.version))
        basis=min(Decimal(state['cash']['liquidation_equity_usd']),Decimal(state['day_start_equity']))
        risk=sum((Decimal(p['open_risk_usd']) for p in state['positions'].values()),Decimal(0))
        settled=max(Decimal(0),min(Decimal(500),packet.settled)-Decimal(state['cash']['reserved_usd']))
        from .real_feedback import economic_feedback,owner_review_feedback,trading_manual_feedback
        from .real_daily import scope
        runtime_scope=json.dumps(dict(kind='runtime_scope_snapshot',source='Installed code; session/account authority independently validated',
            scope=scope()),separators=(',',':'))
        from .decision_api.brain import DECISION_MODEL,is_claude
        if is_claude(DECISION_MODEL):
            # Claude: playbook and rules live in the cached system prompt; the
            # per-call payload carries only scope and learning memory.
            from .real_feedback import learning_memory
            feedback=(runtime_scope,)+learning_memory(state,self.store,self.clock())
        else:
            feedback=owner_review_feedback()+trading_manual_feedback()+(runtime_scope,)+economic_feedback(state,self.store,self.clock())
        return packet.context.model_copy(update=dict(instruments=packet.context.instruments,positions=tuple(positions),
            settled_cash_usd=settled,remaining_risk_usd=max(Decimal(0),basis*__import__('autonomous_trader.economic_policy',fromlist=['effective_policy']).effective_policy(state).open_risk-risk),
            reconciliation_ready=state['reconciliation']['ready'],immutable_boundaries=BOUNDARIES,
            recent_outcomes=feedback))

    def _call(self,context,mode):
        brain=self.brain_factory();brain.call_category='PORTFOLIO_ENTRY' if mode=='ENTRY' else 'FAST_POSITION'
        from .real_feedback import supplied_feedback_identity,supplied_manual_identity
        feedback_identity=supplied_feedback_identity(context)
        manual_identity=supplied_manual_identity(context)
        def retain(value):
            self.emit('REAL_PAID_DECISION_EVIDENCE',dict(value,mode=mode,
                owner_review_feedback=feedback_identity,trading_manual=manual_identity,
                model_work_id=brain.last_call.get('model_work_id'),
                context_as_of=context.as_of.isoformat(),context_expires_at=context.expires_at.isoformat(),
                context_quotes=[dict(symbol=q.symbol,instrument_id=q.instrument_id,
                    quote_at=q.quote_at.isoformat()) for q in context.instruments]))
        brain.decision_evidence_sink=retain
        # Snapshot semantics here; this runner validates refreshed execution
        # context independently in _apply before any gateway authority.
        brain.defer_execution_context_validation=True
        try:
            result=brain.fast_decide(context,mode=mode,hard_policy=BOUNDARIES,clock=self.clock)
        except Exception as exc:
            self.emit('REAL_PAID_DECISION_VALIDATION',dict(mode=mode,
                owner_review_feedback=feedback_identity,trading_manual=manual_identity,
                model_work_id=brain.last_call.get('model_work_id'),outcome='REJECTED',
                validation_stage=brain.last_call.get('validation_stage'),
                reason=_reconciliation_failure_subcode(exc,'MODEL_VALIDATION')))
            raise
        self.emit('REAL_PAID_DECISION_VALIDATION',dict(mode=mode,
            owner_review_feedback=feedback_identity,trading_manual=manual_identity,
            model_work_id=brain.last_call.get('model_work_id'),outcome='MODEL_VALIDATED'))
        result['model_response_at']=self.clock().isoformat()
        result['model_work_id']=brain.last_call.get('model_work_id')
        if mode=='ENTRY':
            checked=self._second_brain_check(context,result,getattr(brain,'budget',None))
            if checked is not None:return checked
        elif mode=='POSITION':
            checked=self._second_brain_close_check(context,result,getattr(brain,'budget',None))
            if checked is not None:return checked
        self._shadow(context,mode,result,getattr(brain,'budget',None))
        return result

    def _second_brain_check(self,context,primary,budget):
        """Dual brain, consensus stage (second_brain.MODE == 'CONSENSUS'), risk-reducing only: an Opus OPEN is
        placed only if the second model also opens the same instrument, merged to the smaller size, lower limit,
        tighter stop and earlier deadlines (decision_api.dual_brain). Disagreement abstains. If the second model
        is unavailable (no key, timeout, error) Opus decides alone. The second model can never open, add,
        close or modify by itself. Returns the result to apply, or None when consensus is off."""
        try:
            from .decision_api import second_brain
            if not second_brain.consensus():return None
            if not isinstance(primary.get('decision'),dict) or primary['decision'].get('action')!='OPEN':return None
            brain,error=second_brain.make_second_brain(budget)
            second=None
            if brain is not None:
                try:
                    brain.call_category='PORTFOLIO_ENTRY';brain.defer_execution_context_validation=True
                    second=brain.fast_decide(context,mode='ENTRY',hard_policy=BOUNDARIES,clock=self.clock)
                except Exception as exc:
                    error=str(exc) if str(exc).isupper() else type(exc).__name__
            from .decision_api.dual_brain import combine_entry
            def parsed(value):
                try:return parse_alpha_decision(json.dumps(value['decision'])) if isinstance(value,dict) else None
                except Exception:return None
            answered=parsed(second)
        except Exception:return None      # before the second model answered: the primary decides alone
        try:
            combined=combine_entry(parsed(primary),answered)
            result=dict(primary)
            result['decision']=combined.decision.model_dump(mode='json')
            result['dual_brain']=dict(mode=combined.mode,second_model=second_brain.SECOND_MODEL,second_error=error)
            if isinstance(second,dict) and second.get('estimated_cost_usd') is not None and primary.get('estimated_cost_usd') is not None:
                result['estimated_cost_usd']=str(Decimal(str(primary['estimated_cost_usd']))+Decimal(str(second['estimated_cost_usd'])))
        except Exception:
            if answered is None:return None
            # The second model answered but combining failed: fail closed (no entry), never trade past a veto.
            result=dict(primary);result['decision']={'schema_version':'1.0','action':'ABSTAIN',
                'reason':'DUAL_BRAIN_COMBINE_FAILED','open':None,'hold':None,'modify':None,'close':None}
            combined=None
        try:
            record=second_brain.shadow_record('ENTRY',context,primary,second,error)
            record.update(authority='CONSENSUS_VETO_ONLY',consensus_mode=combined.mode if combined else 'COMBINE_FAILED')
            self.emit('REAL_DUAL_BRAIN_DECISION',record)
        except Exception:pass
        return result

    def _second_brain_close_check(self,context,primary,budget):
        """Sell-side consensus (owner-approved 2026-10-19): an Opus planned CLOSE is placed only if Astra also says
        CLOSE; otherwise the position is held and reviewed again. Never sells by itself; stops, deadlines and
        safety exits are not reviews and are unaffected. If Astra is unavailable Opus decides alone (as for
        entries). Returns the result to apply, or None (not a close / consensus off)."""
        try:
            from .decision_api import second_brain
            if not second_brain.consensus():return None
            if not isinstance(primary.get('decision'),dict) or primary['decision'].get('action')!='CLOSE':return None
            brain,error=second_brain.make_second_brain(budget)
            second=None
            if brain is not None:
                try:
                    brain.call_category='FAST_POSITION';brain.defer_execution_context_validation=True
                    second=brain.fast_decide(context,mode='POSITION',hard_policy=BOUNDARIES,clock=self.clock)
                except Exception as exc:
                    error=str(exc) if str(exc).isupper() else type(exc).__name__
            from .decision_api.dual_brain import consensus_close
            def parsed(value):
                try:return parse_alpha_decision(json.dumps(value['decision'])) if isinstance(value,dict) else None
                except Exception:return None
            first=parsed(primary)
            if first is None:return None
            combined=consensus_close(first,parsed(second),self.clock()+timedelta(minutes=15))
            if combined.decision.action=='HOLD':
                # The held review must validate at apply time (now < review <= position deadline/horizon), or a
                # rejected HOLD would re-trigger a paid review every cycle.
                held=next((x for x in context.positions if x.position_id==first.close.position_id),None)
                if held is None:return None
                ceiling=min(held.latest_exit_at,context.exit_horizon)
                floor=self.clock()+timedelta(minutes=5)
                review=min(max(combined.decision.hold.next_review_at,floor),ceiling)
                if review<=self.clock()+timedelta(seconds=30):return None   # no valid hold window: primary decides
                combined=type(combined)(combined.decision.model_copy(update={'hold':combined.decision.hold.model_copy(
                    update={'next_review_at':review})}),combined.mode,combined.challenge,combined.note)
            result=dict(primary)
            result['decision']=combined.decision.model_dump(mode='json')
            result['dual_brain']=dict(mode=combined.mode,second_model=second_brain.SECOND_MODEL,second_error=error)
            if isinstance(second,dict) and second.get('estimated_cost_usd') is not None and primary.get('estimated_cost_usd') is not None:
                result['estimated_cost_usd']=str(Decimal(str(primary['estimated_cost_usd']))+Decimal(str(second['estimated_cost_usd'])))
        except Exception:return None      # any failure before combining: the primary decides alone
        try:
            record=second_brain.shadow_record('POSITION',context,primary,second,error)
            record.update(authority='CONSENSUS_CLOSE',consensus_mode=combined.mode,
                consensus_action=combined.decision.action)
            self.emit('REAL_DUAL_BRAIN_DECISION',record)
        except Exception:pass
        return result

    def _shadow(self,context,mode,primary,budget):
        """Dual brain, shadow stage: ask the second model the same question in the background and record
        both answers plus the consensus combiner's verdict. No authority; never delays or alters the primary."""
        try:
            from .decision_api import second_brain
            if not second_brain.enabled():return
            day=self.clock().astimezone(NEW_YORK).date().isoformat()
            counts=self.__dict__.setdefault('shadow_calls',{})
            if counts.get(day,0)>=second_brain.SHADOW_CALLS_PER_SESSION:return
            counts[day]=counts.get(day,0)+1
            if getattr(self,'shadow_worker',None) is None:
                self.shadow_worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix='real-second-brain')
            def run():
                second,error=None,None
                brain,error=second_brain.make_second_brain(budget)
                if brain is not None:
                    try:
                        brain.call_category='PORTFOLIO_ENTRY' if mode=='ENTRY' else 'FAST_POSITION'
                        brain.defer_execution_context_validation=True
                        second=brain.fast_decide(context,mode=mode,hard_policy=BOUNDARIES,clock=lambda:context.as_of)
                    except Exception as exc:
                        error=str(exc) if str(exc).isupper() else type(exc).__name__
                self.emit('REAL_SECOND_BRAIN_SHADOW',second_brain.shadow_record(mode,context,primary,second,error))
            self.shadow_worker.submit(run)
        except Exception:pass

    def _start(self,context,mode,plan_id=None):
        if getattr(self.controller,'waiting_for_market_data',False):raise ValueError('REAL_MARKET_DATA_NOT_READY')
        self.pending=(self.worker.submit(self._call,context,mode),context,mode,plan_id)

    def _authority_guard(self,now):
        receipt=getattr(self,'authority_receipt',None)
        if not receipt:return
        expires=datetime.fromisoformat(receipt['expires_at'])
        if now+timedelta(seconds=60)>=expires:
            state=self.controller.state
            # Carried (charter-authorized) holdings at the close are normal, not an incident.
            if (receipt.get('daily_authority_sha256') and (not state['positions'] or self._all_positions_carry())
                    and not state['reservations']
                    and not any(not row['final'] or row.get('phase')=='UNKNOWN' for row in state['orders'].values())):
                self.controller.recovery_only=True
                return
            self.controller.downgrade_to_recovery('REAL_ENTRY_AUTHORITY_EXPIRING')

    def _position_plan(self,iid):
        state=self.controller.state
        return self.plan(state['orders'][state['positions'][iid]['originating_orders'][0]]['intent']['plan_id'])

    def _all_positions_carry(self):
        """Every held position has a charter-authorized plan that outlives this session."""
        try:
            session=reviewed_calendar().session(self.clock().astimezone(NEW_YORK).date())
            return bool(session) and all(plan_carries(self._position_plan(iid),session)
                for iid in self.controller.state['positions'])
        except Exception:return False

    def _post_close_exact(self,account):
        state=self.controller.state
        local_clients={row['intent']['client_order_id'] for row in state['orders'].values() if not row['final']}
        broker_positions={row['symbol']:Decimal(row['quantity']) for row in account['positions'] if Decimal(row['quantity'])!=0}
        local_positions={state['instruments'][iid]['ticker']:Decimal(row['quantity']) for iid,row in state['positions'].items()}
        if set(account['open_client_ids'])!=local_clients:raise ValueError('REAL_POST_CLOSE_ORDER_MISMATCH')
        if broker_positions!=local_positions:raise ValueError('REAL_POST_CLOSE_POSITION_MISMATCH')
        # Owner decision 2026-10-14: multi-day holds. Only plans authorized by the signed charter may carry.
        if local_positions and not self._all_positions_carry():raise ValueError('REAL_EXPOSURE_OUTSIDE_SESSION')
        # Broker-truth (owner 2026-10-18): the broker's cash is the balance; adopt it before comparing.
        try:
            confirm_flat_settlements(self.controller,{**account,'evidence_id':account.get('evidence_id') or uuid4().hex},emit=self.emit)
            state=self.controller.state
        except Exception:pass
        cash=account['cash'];settled=Decimal(cash['settled_cash']);unsettled=Decimal(cash['unsettled_cash'])
        total=Decimal(cash['cash_balance']);equity=Decimal(cash['net_liquidation_value'])
        durable_settled=Decimal(state['cash']['settled_usd']);durable_unsettled=Decimal(state['cash']['unsettled_usd'])
        receivables=sum((Decimal(row['outstanding_usd']) for row in state['settlements'].values()),Decimal(0))
        # Broker-truth adoptions carry part of the unsettled bucket (owner 2026-10-18).
        receivables+=sum((Decimal(a['unsettled']) for a in state.get('cash_adoptions',{}).values()),Decimal(0))
        durable_equity=Decimal(state['cash']['liquidation_equity_usd'])
        failed=tuple(name for name,bad in (
            ('broker_settled_vs_ledger',settled!=durable_settled),
            ('broker_unsettled_vs_ledger',unsettled!=durable_unsettled),
            ('broker_unsettled_vs_receivables',unsettled!=receivables),
            ('broker_total_vs_parts',total!=settled+unsettled),
            ('broker_total_vs_ledger_parts',total!=durable_settled+durable_unsettled),
            # With carried positions equity moves with marks; cash buckets and quantities stay exact.
            ('broker_equity_vs_ledger',not local_positions and equity!=durable_equity)) if bad)
        if failed:
            # Diagnostics: cash figures only (no identifiers or secrets); "crash log or it did not happen".
            # Best effort: diagnostics must never weaken or replace the exact mismatch refusal below.
            try:
                self.emit('REAL_POST_CLOSE_MISMATCH_DETAIL',dict(failed=failed,
                    broker=dict(settled=str(settled),unsettled=str(unsettled),total=str(total),equity=str(equity)),
                    ledger=dict(settled=str(durable_settled),unsettled=str(durable_unsettled),
                        receivables=str(receivables),equity=str(durable_equity))),
                    key='real-postclose-mismatch-'+str(self.controller.account_id)+'-'+str(state['session']['label'])
                        +'-'+','.join(failed)+'-'+str(settled)+'-'+str(unsettled))
            except Exception:pass
            raise ValueError('REAL_POST_CLOSE_CASH_SETTLEMENT_MISMATCH')
        label=(state.get('session') or {}).get('label')
        for oid,order in state['orders'].items():
            if not order['final']:continue
            intent=OrderIntent.model_validate(order['intent'])
            # Only this session's orders (owner-approved 2026-10-19): earlier days' orders were verified at their
            # own close; re-reading the whole history every close exhausts Webull's order-detail quota.
            if label and intent.deadline.astimezone(NEW_YORK).date().isoformat()!=str(label):continue
            body=self.gateway.transport.detail(intent.client_order_id)
            rows=body.get('orders',[]) if isinstance(body,dict) else []
            if len(rows)!=1 or not rows[0].get('order_id'):raise ValueError('REAL_POST_CLOSE_ORDER_HISTORY_MISMATCH')
            self.gateway.reconcile_order(intent,state['instruments'][intent.instrument_id]['ticker'],
                rows[0]['order_id'],refresh=False,body=body)

    def _monitor_owned_safety(self,session):
        """Evaluate every held symbol from the narrow direct quote path."""
        submitted=[]
        for iid in tuple(self.controller.state['positions']):
            try:
                state=self.controller.state;p=state['positions'][iid]
                plan=self.plan(state['orders'][p['originating_orders'][0]]['intent']['plan_id'])
                carried=plan.latest_exit_at<session.opens_at
                # A charter-authorized multi-day plan exits at its own deadline, not at today's close.
                multi_day=plan_carries(plan,session)
                deadline=plan.latest_exit_at if multi_day else min(plan.latest_exit_at,session.liquidation_at)
                # A 'daily' latch journaled in an earlier session is not today's loss: the ledger clears it when
                # today's session opens (NEW_SESSION). It never sells today on yesterday's result.
                stale_daily=bool(state.get('session')) and state['session']['label']<session.label.isoformat()
                latched=any(state['latches'].get(k) for k in LIQUIDATING_LATCHES if not (k=='daily' and stale_daily))
                reason=('SAFETY_LATCH' if latched else 'MANDATORY_SAME_DAY_EXIT'
                    if not carried and self.clock()>=deadline
                    else 'SESSION_LIQUIDATION' if not multi_day and self.clock()>=session.liquidation_at else None)
                if reason:
                    packet=self.source.ownership_packet(self.controller.state,iid)
                    self.gate.check_exit_ownership(packet,self.clock(),self.controller.state)
                    packet=self.gateway.cancel_owned_buys_for_exit(iid,packet,reason)
                    state=self.controller.state
                    if iid not in state['positions']:continue
                    p=state['positions'][iid]
                else:
                    packet=self.source.exit_packet(self.controller.state,iid)
                    self.gate.check_exit_observation(packet,self.clock(),self.controller.state)
                    if packet.bid_usd<=plan.active_stop_usd:reason='PROTECTIVE_STOP'
                if reason:
                    if any(not o.get('final',True) and o['intent'].get('instrument_id')==iid and o['intent'].get('side')=='SELL_OWNED'
                            and o['intent'].get('order_type')=='LIMIT' for o in self.controller.state['orders'].values()):
                        # A resting planned-exit LIMIT must not delay a safety exit (stop, deadline, latch).
                        if self.gateway.cancel_planned_sells_for_safety(iid,reason):
                            waits=self.__dict__.setdefault('safety_cancel_waits',{})
                            waits[iid]=waits.get(iid,0)+1
                            if waits[iid]>=SAFETY_CANCEL_WAIT_CYCLES:
                                waits[iid]=0
                                raise ValueError('REAL_EXIT_SELL_CANCEL_UNCERTAIN')   # alert; keep trying
                            submitted.append('SAFETY_EXIT_AWAITING_CANCEL');continue   # MARKET next cycle
                        self.__dict__.setdefault('safety_cancel_waits',{}).pop(iid,None)
                        state=self.controller.state
                        if iid not in state['positions']:continue
                        p=state['positions'][iid]
                        # Fresh evidence after the cancel (the earlier packet still lists the cancelled order).
                        if reason=='PROTECTIVE_STOP':
                            packet=self.source.exit_packet(state,iid)
                            self.gate.check_exit_observation(packet,self.clock(),state)
                        else:
                            packet=self.source.account_order_packet(state,iid)
                            self.gate.check_buy_cancel(packet,self.clock(),state)
                    reserved=sum((Decimal(row['shares']) for row in state['reservations'].values()
                        if row['instrument_id']==iid),Decimal(0))
                    available=Decimal(p['quantity'])-reserved
                    if available>0:
                        self.gateway.protective_exit(plan,reason,quantity=available,packet=packet)
                        submitted.append(reason)
                elif plan.exit_request and self._planned_exit(plan,plan.exit_request,packet=packet):
                    submitted.append('PLANNED_EXIT')
            except Exception as exc:
                code=str(exc) if str(exc) in ('REAL_EXIT_BUY_CANCEL_UNCERTAIN','REAL_EXIT_SELL_CANCEL_UNCERTAIN') else 'REAL_HELD_POSITION_MONITOR_FAILED'
                self._incident(code,instrument_id=iid)
        return submitted

    def _track_excursion(self,plan,bid):
        """Best/worst bid while held, for the learning feed (MFE/MAE). Best effort, throttled to 0.25% moves."""
        try:
            seen=self.__dict__.setdefault('excursions',{})
            high,low=seen.get(plan.plan_id,(bid,bid));high,low=max(high,bid),min(low,bid)
            last=self.__dict__.setdefault('excursions_emitted',{}).get(plan.plan_id)
            seen[plan.plan_id]=(high,low)
            if last and abs(high-last[0])<last[0]*Decimal('.0025') and abs(low-last[1])<last[1]*Decimal('.0025'):return
            self.excursions_emitted[plan.plan_id]=(high,low)
            self.emit('REAL_POSITION_EXCURSION',dict(plan_id=plan.plan_id,symbol=plan.symbol,high_bid=str(high),
                low_bid=str(low),entry_limit_usd=str(plan.entry_limit_usd)),
                key='real-excursion-'+plan.plan_id+'-'+str(high)+'-'+str(low))
        except Exception:pass

    def _planned_exit(self,plan,reason,*,packet=None):
        """Execute a validated decision-model exit: LIMIT at the current bid (never below the market),
        cancelled after PLANNED_EXIT_TTL and repeated; MARKET after PLANNED_EXIT_LIMIT_ATTEMPTS attempts.
        Returns the submitted reason, or None when nothing was sent (attempt in flight / nothing free)."""
        from .real_strategy_gateway import PLANNED_EXIT_LIMIT_ATTEMPTS
        state=self.controller.state;iid=plan.instrument_id
        if iid not in state['positions']:return None
        if plan.exit_request is None:
            # Record the decision first: a CLOSE that cannot be placed right now (order in flight, near the
            # close) is executed later by the monitor, never silently dropped.
            plan=AlphaManagedPlan.model_validate({**plan.model_dump(),'exit_request':reason[:1000],'version':plan.version+1})
            self.emit('REAL_PLAN',dict(plan_id=plan.plan_id,plan=plan.model_dump(mode='json'),original=False,
                reason='EXIT_REQUESTED: '+reason[:900]))
        if any(not o['final'] and o['intent']['instrument_id']==iid and o['intent']['side']=='SELL_OWNED'
                for o in state['orders'].values()):return None
        reserved=sum((Decimal(row['shares']) for row in state['reservations'].values() if row['instrument_id']==iid),Decimal(0))
        available=Decimal(state['positions'][iid]['quantity'])-reserved
        if available<=0:return None
        session=reviewed_calendar().session(self.clock().astimezone(NEW_YORK).date())
        if not session or not session.contains(self.clock()):return None
        near_close=self.clock()>=session.closes_at-timedelta(minutes=3)
        # A multi-day plan's exit waits for the next open rather than leaving an order across the close.
        if near_close and plan_carries(plan,session):return None
        packet=packet if packet is not None and getattr(packet,'bid_usd',None) is not None else self.source.exit_packet(state,iid)
        self.gate.check_exit_observation(packet,self.clock(),self.controller.state)
        # Protection-only mode (no session certificate) cannot cancel an unfilled LIMIT, so it sells at market.
        escalate=(plan.exit_attempts>=PLANNED_EXIT_LIMIT_ATTEMPTS or near_close
            or getattr(self.controller,'protection_only',False))
        updated=AlphaManagedPlan.model_validate({**plan.model_dump(),'exit_request':reason[:1000],
            'exit_attempts':plan.exit_attempts+1,'version':plan.version+1})
        self.emit('REAL_PLAN',dict(plan_id=plan.plan_id,plan=updated.model_dump(mode='json'),original=False,
            reason=('PLANNED_EXIT_MARKET' if escalate else 'PLANNED_EXIT_LIMIT')+' attempt '+str(updated.exit_attempts)))
        if escalate:
            self.gateway.protective_exit(updated,'PLANNED_EXIT_MARKET: '+reason[:900],quantity=available,packet=packet)
            return 'PLANNED_EXIT_MARKET'
        self.gateway.protective_exit(updated,'PLANNED_EXIT_LIMIT: '+reason[:900],quantity=available,packet=packet,
            limit_usd=packet.bid_usd)
        return 'PLANNED_EXIT_LIMIT'

    def _cancel_latched_buys(self):
        buys=[]
        for row in self.controller.state['orders'].values():
            if row['final']:continue
            intent=OrderIntent.model_validate(row['intent'])
            if intent.side=='BUY':buys.append(intent)
        for intent in buys:
            try:
                packet=self.source.account_order_packet(self.controller.state,intent.instrument_id)
                self.gateway.cancel_owned_buys_for_exit(intent.instrument_id,packet,'SAFETY_LATCH')
            except Exception:
                return self._incident('REAL_LATCH_BUY_CANCEL_UNCERTAIN',intent_id=intent.intent_id)
        return None

    def _entry_slot(self,session,packet):
        """(key, slot evidence) when an entry call is justified, else (None, None)."""
        from .real_triggers import entry_trigger,reference,trigger_token
        now=self.clock();current=reference(packet.context)
        try:
            with self.store.db.connect() as c:
                row=c.execute("SELECT payload FROM events WHERE kind='REAL_PORTFOLIO_SLOT' AND payload->>'session'=%s AND payload ? 'reference' ORDER BY at DESC LIMIT 1",
                    (session.label.isoformat(),)).fetchone()
            last=row[0] if row and isinstance(row[0],dict) and isinstance(row[0].get('reference'),dict) else None
            reason=entry_trigger(last,current,now)
            if reason is None:return None,None
            key=portfolio_context_key(self.controller.account_id,session,packet,trigger_token(reason,now))
            with self.store.db.connect() as c:
                if c.execute('SELECT 1 FROM events WHERE id=%s',(key,)).fetchone():return None,None
        except (psycopg_errors.QueryCanceled,psycopg_errors.LockNotAvailable):
            # A slow or locked ledger read fails closed for this one cycle; it must never crash the trader.
            self.entry_slot_db_skips=getattr(self,'entry_slot_db_skips',0)+1
            return None,None
        return key,dict(trigger=reason,at=now.isoformat(),reference=current)

    def _record_completed_abstention(self):
        if not self.pending or self.pending[2]!='ENTRY' or not self.pending[0].done():return False
        try:result=self.pending[0].result()
        except Exception:return False
        if not isinstance(result,dict) or not isinstance(result.get('decision'),dict):return False
        if result['decision'].get('action')!='ABSTAIN':return False
        self._apply(None)
        return True

    def _apply(self,packet):
        future,original,mode,reviewed_plan=self.pending
        if not future.done():return
        self.pending=None
        result=None
        try:
            result=future.result();decision=parse_alpha_decision(json.dumps(result['decision']))
            if mode=='ENTRY' and decision.action=='ABSTAIN':
                # Evidence only: validate the original supplied context, never grant execution authority.
                validate_alpha_semantics(decision,original,original.as_of,maximum_quote_age_ms=10000)
            else:
                context=self.context(packet)
                if mode=='POSITION' and decision.action=='OPEN' and reviewed_plan:
                    # An add inherits the position's deadline; validate what will actually be placed.
                    held_plan=self.plan(reviewed_plan)
                    decision=decision.model_copy(update={'open':decision.open.model_copy(update={
                        'latest_exit_at':held_plan.latest_exit_at,
                        'next_review_at':min(decision.open.next_review_at,held_plan.latest_exit_at)})})
                validate_alpha_semantics(decision,context,self.clock(),maximum_quote_age_ms=10000)
        except Exception as exc:
            raw=str(exc)
            code=(raw if isinstance(exc,ValueError) and raw and raw.upper()==raw
                and raw.replace('_','').replace('-','').isalnum() else 'REAL_MODEL_DECISION_FAILED')
            if isinstance(result,dict):
                self.emit('REAL_PAID_DECISION_VALIDATION',dict(mode=mode,
                    model_work_id=result.get('model_work_id'),outcome='REJECTED',
                    validation_stage='CURRENT_CONTEXT',reason=code))
            if mode=='POSITION' and reviewed_plan:
                transient=code in ('MODEL_CALL_BUDGET_WAIT','MODEL_PROVIDER_BACKPRESSURE')
                until=self.clock() if not transient else self.clock()+timedelta(seconds=60)
                self.position_call_blocked_until[reviewed_plan]=min(until,original.session_close)
            self.emit('REAL_MODEL_CALL_BLOCKED',{'summary':code,'mode':mode,'plan_id':reviewed_plan},
                key='real-model-block-'+mode+'-'+str(reviewed_plan or original.session_open.date())+'-'+code)
            return
        if mode=='ENTRY':
            if decision.action not in ('ABSTAIN','OPEN'):raise ValueError('REAL_ENTRY_ACTION')
            usage=result['usage'];cost=result['estimated_cost_usd']
            if cost is None:raise ValueError('REAL_MODEL_COST_UNRESOLVED')
            envelope=AlphaDecisionEnvelope(decision_id=uuid4().hex,context_hash=payload_hash(original),
                created_at=datetime.fromisoformat(result['model_response_at']) if result.get('model_response_at') else self.clock(),
                valid_until=original.expires_at,policy_version='real-hard-policy-v1',
                capability_version=CAPABILITY_VERSION,model=result['model'],reasoning=result['reasoning'],
                input_tokens=usage['input_tokens'],output_tokens=usage['output_tokens'],estimated_cost_usd=cost,decision=decision)
            record=build_portfolio_decision_evidence(original,decision,envelope,result)
            persist_portfolio_decision_evidence(self.store,record,mode='REAL',at=self.clock())
            if decision.action=='ABSTAIN':return
            original_positions={p.instrument_id:p.quantity for p in original.positions}
            if original_positions!={p.instrument_id:p.quantity for p in context.positions}:raise ValueError('REAL_CONTEXT_ACCOUNT_CHANGED')
            decision=self._snap_open(decision)
            decision=self._allocate(decision,context,envelope.decision_id)
            if decision is None:return
            # One risk episode per round trip: a new position starts a fresh planned-loss allowance under the policy
            # in force; an entry on a held symbol joins that position's episode (the ledger requires it).
            held=self.controller.state['positions'].get(decision.open.instrument_id)
            episode=held['episode_id'] if held else 'alpha-'+decision.open.instrument_id+'-'+envelope.decision_id[:16]
            intent,plan=translate_open(decision,context,self.controller.account_id,self.clock(),envelope.decision_id,
                capability_version=CAPABILITY_VERSION,model_cost_usd=Decimal(cost),episode_id=episode)
            protection=intent.protection.model_copy(update={'trigger_source_id':SOURCE})
            intent=OrderIntent.model_validate({**intent.model_dump(),'environment':'REAL','protection':protection})
            self.emit('REAL_PLAN',dict(plan_id=plan.plan_id,plan=plan.model_dump(mode='json'),original=True,
                decision_evidence=record.model_dump(mode='json'),max_planned_loss_usd=str(plan.quantity*(plan.entry_limit_usd-plan.original_stop_usd)),
                invalidation=plan.thesis,profit_plan_usd=None if plan.target_usd is None else str(plan.quantity*(plan.target_usd-plan.entry_limit_usd))),
                key='real-original-'+plan.plan_id)
            self.gateway.submit(intent,plan)
        else:
            plan=self.plan(reviewed_plan)
            if plan.instrument_id not in self.controller.state['positions']:return
            if decision.action=='CLOSE':
                # Owner decision 2026-10-15: planned exits are limit orders not below the market.
                self._planned_exit(plan,'MODEL_CLOSE: '+decision.reason)
                return
            if decision.action=='OPEN':
                o=decision.open
                if o.instrument_id!=plan.instrument_id or o.hard_stop_usd!=plan.active_stop_usd:
                    # A malformed add is a rejected decision, never a crash or an order.
                    self.emit('REAL_PAID_DECISION_VALIDATION',dict(mode=mode,model_work_id=result.get('model_work_id'),
                        outcome='REJECTED',validation_stage='ADD_SHARES',reason='REAL_ADD_REQUIRES_HELD_INSTRUMENT_AND_ACTIVE_STOP'))
                    return
                self._add_shares(plan,decision,result,context)
                return
            updates={'version':plan.version+1}
            session=reviewed_calendar().session(self.clock().astimezone(NEW_YORK).date())
            if session and plan.latest_exit_at<session.opens_at:updates['latest_exit_at']=session.liquidation_at
            if decision.hold:updates['next_review_at']=decision.hold.next_review_at
            elif decision.modify:
                m=decision.modify
                if m.requested_stop_usd is not None:
                    updates['active_stop_usd']=m.requested_stop_usd
                    self.controller.send(Protection(instrument_id=plan.instrument_id,
                        quantity=self.controller.state['positions'][plan.instrument_id]['quantity'],stop_usd=m.requested_stop_usd,evidence_id=uuid4().hex))
                if m.target_action=='SET':updates['target_usd']=m.requested_target_usd
                elif m.target_action=='REMOVE':updates['target_usd']=None
                if m.requested_next_review_at:updates['next_review_at']=m.requested_next_review_at
                if m.requested_latest_exit_at:updates['latest_exit_at']=snap_exit(m.requested_latest_exit_at,session)
                if 'latest_exit_at' in updates and updates.get('next_review_at',plan.next_review_at)>updates['latest_exit_at']:
                    updates['next_review_at']=updates['latest_exit_at']
            else:raise ValueError('REAL_POSITION_ACTION')
            # A deadline renewed by a validated decision (or reset for a carried plan) raises the plan's ceiling.
            if 'latest_exit_at' in updates and updates['latest_exit_at']>plan.original_latest_exit_at:
                updates['original_latest_exit_at']=updates['latest_exit_at']
            updated=AlphaManagedPlan.model_validate({**plan.model_dump(),**updates})
            self.emit('REAL_PLAN',dict(plan_id=plan.plan_id,plan=updated.model_dump(mode='json'),original=False,reason=decision.reason))

    def _allocate(self,decision,context,decision_id):
        """Strategy-weighted sizing: shrink (never grow) a new entry by its strategy's realized REAL record."""
        from .strategy_allocation import label,scale_open,weights
        from .monitoring.views import trades_from_state
        o=decision.open;name=label(o.strategy_label)
        try:
            with self.store.db.connect() as c:
                rows=c.execute("SELECT payload FROM events WHERE mode='REAL' AND kind='REAL_PLAN' AND payload->>'original'='true' ORDER BY at DESC LIMIT 500").fetchall()
            plans={row[0]['plan_id']:row[0]['plan'] for row in rows}
            w,why=weights(trades_from_state(self.controller.state,{},plans)).get(name,(Decimal(1),'NEW_STRATEGY_FULL_SIZE'))
        except Exception as exc:
            w,why=Decimal(1),'ALLOCATION_UNAVAILABLE:'+type(exc).__name__
        if w>=1:return decision
        increment=next((Decimal(str(i.quantity_increment)) for i in context.instruments if i.instrument_id==o.instrument_id),Decimal(1))
        scaled=scale_open(o,w,increment)
        self.emit('REAL_STRATEGY_ALLOCATION',dict(decision_id=decision_id,strategy=name,weight=str(w),reason=why,
            requested_quantity=None if o.desired_quantity is None else str(o.desired_quantity),
            requested_notional_usd=None if o.desired_notional_usd is None else str(o.desired_notional_usd),
            placed=scaled is not None))
        return None if scaled is None else decision.model_copy(update={'open':scaled})

    def _snap_open(self,decision):
        session=reviewed_calendar().session(self.clock().astimezone(NEW_YORK).date())
        o=decision.open;latest=snap_exit(o.latest_exit_at,session)
        if latest==o.latest_exit_at:return decision
        return decision.model_copy(update={'open':o.model_copy(update={'latest_exit_at':latest,
            'next_review_at':min(o.next_review_at,latest)})})

    def _add_shares(self,plan,decision,result,context):
        """Position-review OPEN on the held instrument = add shares (e.g. average down with a live thesis).
        The add keeps the position's active stop and deadline; the ledger enforces per-stock exposure and the
        episode's total planned-loss allowance across the original entry and every add."""
        o=decision.open;state=self.controller.state;now=self.clock()
        next_review=min(max(o.next_review_at,now+timedelta(minutes=1)),plan.latest_exit_at)
        # The review is consumed first, so a refused add never re-triggers paid reviews in a loop.
        updated=AlphaManagedPlan.model_validate({**plan.model_dump(),'version':plan.version+1,'next_review_at':next_review})
        def refused(code):
            self.emit('REAL_PLAN',dict(plan_id=plan.plan_id,plan=updated.model_dump(mode='json'),original=False,
                reason='ADD_REFUSED '+code+': '+decision.reason[:800]))
            self.emit('REAL_PAID_DECISION_VALIDATION',dict(mode='POSITION',model_work_id=result.get('model_work_id'),
                outcome='REJECTED',validation_stage='ADD_SHARES',reason=code))
        session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        from .real_strategy_gateway import MIN_REAL_CLOSE_LIFECYCLE
        if getattr(self.controller,'recovery_only',False):return refused('REAL_ADD_RECOVERY_ONLY')
        if any(state['latches'].values()):return refused('REAL_ADD_SAFETY_LATCHED')
        if plan.exit_request:return refused('REAL_ADD_EXIT_IN_PROGRESS')
        if not session or not session.permits_entry(now) or now+MIN_REAL_CLOSE_LIFECYCLE>=min(plan.latest_exit_at,session.liquidation_at):
            return refused('REAL_ADD_OUTSIDE_ENTRY_WINDOW')
        if any(not row['final'] and row['intent']['instrument_id']==plan.instrument_id for row in state['orders'].values()):
            return refused('REAL_ADD_ORDER_IN_FLIGHT')
        try:
            cost=result.get('estimated_cost_usd') or '0'
            adjusted=decision.model_copy(update={'open':o.model_copy(update={'latest_exit_at':plan.latest_exit_at,
                'next_review_at':next_review})})
            from .mvp.alpha import translate_open
            intent,add_plan=translate_open(adjusted,context,self.controller.account_id,now,uuid4().hex,
                capability_version=CAPABILITY_VERSION,model_cost_usd=Decimal(cost),
                episode_id=state['positions'][plan.instrument_id]['episode_id'])
            protection=intent.protection.model_copy(update={'trigger_source_id':SOURCE})
            intent=OrderIntent.model_validate({**intent.model_dump(),'environment':'REAL','protection':protection})
        except Exception as exc:
            return refused(str(exc) if str(exc).isupper() else 'REAL_ADD_TRANSLATION_FAILED')
        self.emit('REAL_PLAN',dict(plan_id=plan.plan_id,plan=updated.model_dump(mode='json'),original=False,
            reason='ADD '+str(intent.quantity)+' @'+str(intent.limit_price_usd)+': '+decision.reason[:800]))
        self.emit('REAL_PLAN',dict(plan_id=add_plan.plan_id,plan=add_plan.model_dump(mode='json'),original=True,
            add_to_plan_id=plan.plan_id,reason='ADD: '+decision.reason[:900]),key='real-original-'+add_plan.plan_id)
        try:self.gateway.submit(intent,add_plan)
        except Exception as exc:
            # Limits (exposure, episode risk, cash) are enforced by the ledger before any broker write; a refusal
            # is evidence, never a crash. An uncertain submission has already opened its own incident.
            self.emit('REAL_PAID_DECISION_VALIDATION',dict(mode='POSITION',model_work_id=result.get('model_work_id'),
                outcome='REJECTED',validation_stage='ADD_SUBMIT',
                reason=str(exc)[:120] if str(exc).replace(',','').replace(':','').replace('_','').replace('-','').isalnum() and str(exc).isupper() else type(exc).__name__))

    def _defer_read(self,exc):
        self.read_retry_at=self.clock()+timedelta(seconds=exc.retry_after)
        self.controller.waiting_for_market_data=True
        self.emit('REAL_MARKET_DATA_WAIT',{'reason':str(exc),'endpoint':exc.path,
            'retry_after_seconds':exc.retry_after,'retry_at':self.read_retry_at.isoformat()})
        return 'WAITING_FOR_MARKET_DATA'

    def _wait_for_market_data(self):
        if not clean_flat(self.controller.state):
            self.controller.waiting_for_market_data=False
            self.controller.recovery_only=True
            return self._incident('REAL_WAITING_ACCOUNT_NOT_CLEAN')
        retry_at=getattr(self,'read_retry_at',None)
        if retry_at is not None and self.clock()<retry_at:return 'WAITING_FOR_MARKET_DATA'
        try:
            account={**self.source.account_observation(),'evidence_id':uuid4().hex}
            confirm_flat_settlements(self.controller,account,emit=self.emit)
            flat_recovery_values(account,self.controller.state,self.controller.account_id,self.clock())
            self.controller.refresh_flat_recovery(account)
        except Exception as exc:
            if isinstance(exc,ReadRateLimited):return self._defer_read(exc)
            self.controller.waiting_for_market_data=False
            return self._reconciliation_failure(exc,'ACCOUNT_OBSERVATION')
        try:packet=self.prepare_waiting_market()
        except Exception as exc:
            if isinstance(exc,ReadRateLimited):return self._defer_read(exc)
            if market_unavailable(exc):
                self.emit('REAL_MARKET_DATA_WAIT',{'reason':str(exc)})
                return 'WAITING_FOR_MARKET_DATA'
            self.controller.waiting_for_market_data=False
            return self._reconciliation_failure(exc,'WAITING_CERTIFICATION')
        session=reviewed_calendar().session(self.clock().astimezone(NEW_YORK).date())
        if getattr(self.controller,'recovery_only',False):
            try:return self._resume_normal(packet,session)
            except Exception as exc:
                if isinstance(exc,ReadRateLimited):return self._defer_read(exc)
                if market_unavailable(exc):return 'WAITING_FOR_MARKET_DATA'
                self.controller.waiting_for_market_data=False
                return self._reconciliation_failure(exc,'RECOVERY_NORMAL_READINESS')
        self.controller.waiting_for_market_data=False
        return self._advance_packet(packet,session,prepared=True)

    def _resume_normal(self,packet,session):
        """Local handoff through existing normal authority, never a new approval."""
        if not session or not session.permits_entry(self.clock()):return 'RECOVERY_MONITORING'
        if not clean_flat(self.controller.state) or not self._single_writer():
            raise ValueError('REAL_ACCOUNT_RECOVERY_AUTHORITY')
        receipt=getattr(self,'authority_receipt',{})
        if not receipt.get('daily_authority_sha256') or receipt.get('service_only'):
            raise ValueError('REAL_ACCOUNT_RECOVERY_AUTHORITY')
        # Use the normal gate, not the reduce-only gate. The composition root
        # has just prepared fresh evidence under the current signed authority.
        packet,checkpoint=self.gate.read_checked(
            lambda:self.source.refresh(self.controller.state),self.clock)
        self.controller.lease()
        if not self._refresh_or_wait(packet):return 'WAITING_FOR_MARKET_DATA'
        self._record_reconciliation_success(packet,checkpoint)
        if not clean_flat(self.controller.state) or not self._single_writer():
            raise ValueError('REAL_ACCOUNT_RECOVERY_AUTHORITY')
        if not session.permits_entry(self.clock()):return 'RECOVERY_MONITORING'
        self.emit('REAL_RECOVERY_NORMAL_READY',dict(
            build_sha256=receipt['build_sha256'],market_session_label=receipt['market_session_label']))
        self.controller.recovery_only=False
        self.controller.waiting_for_market_data=False
        # Decisions/orders remain in the ordinary loop with its normal gates.
        return 'RECOVERY_NORMAL_READY'

    def _market_failure_or_incident(self,exc,stage):
        if isinstance(exc,ReadRateLimited) and clean_flat(self.controller.state):return self._defer_read(exc)
        # Holding positions: a rate-limited market refresh waits out Webull's window instead of locking (owner
        # approvals 2026-10-19; 2026-10-27 saw 15 such locks). Stops, deadlines and safety exits do not depend on it:
        # step() runs _monitor_owned_safety on its own reads before this refresh, every cycle.
        if (isinstance(exc,ReadRateLimited) and self.controller.state['positions']
                and not self.controller.state['reservations'] and not any(self.controller.state['latches'].values())):
            self.held_read_retry_at=self.clock()+timedelta(seconds=min(300,max(30,exc.retry_after)))
            self.emit('REAL_MARKET_DATA_WAIT',{'reason':str(exc),'stage':stage,'endpoint':exc.path,
                'held_positions':len(self.controller.state['positions']),'retry_at':self.held_read_retry_at.isoformat()})
            return 'HELD_READ_DEFERRED'
        if market_unavailable(exc) and clean_flat(self.controller.state) and hasattr(self,'prepare_waiting_market'):
            self.controller.waiting_for_market_data=True
            return self._wait_for_market_data()
        return self._reconciliation_failure(exc,stage)

    def _refresh_or_wait(self,packet):
        try:self.controller.refresh(packet)
        except ValueError as exc:
            # Preserve evidence freshness and all exposure protections. A flat
            # account with expired observations must obtain new evidence.
            if (str(exc) not in {'STALE_REAL_ACCOUNT','STALE_REAL_QUOTE','STALE_REAL_CONTEXT'}
                    or not clean_flat(self.controller.state) or not hasattr(self,'prepare_waiting_market')):raise
            self.controller.waiting_for_market_data=True
            self.emit('REAL_MARKET_DATA_WAIT',{'reason':str(exc),'stage':'LEDGER_RECONCILE'})
            return False
        return True

    def _recover_flat_account_incident(self,*,closed=False):
        # Same eligible incident scope, but account evidence does not require quotes.
        if not recoverable_flat_incident(self.controller.state):
            self._reset_reconciliation_successes();return 'SAFETY_LATCHED'
        try:
            from .real_owner_trust import authority_revision
            revision=authority_revision(self.gate.root)
            now=self.clock();receipt=service_receipt(self.gate.root,now,recovery=closed)
            if (receipt['account_binding']!=self.controller.account_id
                    or receipt['build_sha256']!=self.authority_receipt['build_sha256']
                    or receipt['market_session_label']!=now.astimezone(NEW_YORK).date().isoformat()
                    or not self._single_writer()):raise ValueError('REAL_ACCOUNT_RECOVERY_AUTHORITY')
            checkpoint,_=self.gate.checkpoint(receipt['flat_checkpoint_sha256'])
            if self.reconciliation_successes:
                if service_receipt(self.gate.root,self.clock(),recovery=closed)!=receipt:
                    raise ValueError('REAL_ACCOUNT_RECOVERY_AUTHORITY')
            deadline=receipt['recovery_not_after'] if closed else receipt['expires_at']
            if not datetime.fromisoformat(receipt['issued_at'])<=self.clock()<datetime.fromisoformat(deadline):
                raise ValueError('REAL_ACCOUNT_RECOVERY_AUTHORITY')
            account={**self.source.account_observation(),'evidence_id':uuid4().hex}
            # Re-verify the complete authority after the read, not only the file
            # change detector: a revocation, release or certificate change during
            # the read must never let this observation clear an incident.
            if (authority_revision(self.gate.root)!=revision
                    or service_receipt(self.gate.root,self.clock(),recovery=closed)!=receipt
                    or not datetime.fromisoformat(receipt['issued_at'])<=self.clock()<datetime.fromisoformat(deadline)):
                raise ValueError('REAL_ACCOUNT_RECOVERY_AUTHORITY')
            confirm_flat_settlements(self.controller,account,emit=self.emit)
            values=flat_recovery_values(account,self.controller.state,self.controller.account_id,self.clock())
            if values!=resolution_baseline(self.controller.state,checkpoint):
                raise ValueError('REAL_RECONCILIATION_RECOVERY_CASH_MISMATCH')
            self.controller.refresh_flat_recovery(account)
            if not recoverable_flat_incident(self.controller.state):raise ValueError('REAL_RECONCILIATION_INCIDENT_SCOPE_INVALID')
            ids=tuple(sorted(self.controller.state['incidents']))
            observation={'evidence_id':account['evidence_id'],'observed_at':account['observed_at'].isoformat(),
                'incident_ids':list(ids),'authority_sha256':receipt.get('daily_authority_sha256')}
            if self.reconciliation_successes:
                prior=self.reconciliation_successes[-1]
                if (prior.get('incident_ids')!=list(ids) or prior.get('authority_sha256')!=observation['authority_sha256']
                        or datetime.fromisoformat(prior['observed_at'])>=account['observed_at']):
                    self._reset_reconciliation_successes()
            self.reconciliation_successes.append(observation)
            if len(self.reconciliation_successes)<2:return 'RECOVERY_ACCOUNT_CONFIRMATION_PENDING'
            if not self._single_writer():
                raise ValueError('REAL_ACCOUNT_RECOVERY_AUTHORITY')
            evidence=dict(incident_ids=list(ids),observations=self.reconciliation_successes[-2:],
                account_binding=self.controller.account_id,build_sha256=receipt['build_sha256'],
                market_session_label=receipt['market_session_label'],evidence_type='ACCOUNT_ONLY',
                settled_usd=str(values[0]),unsettled_usd=str(values[1]),equity_usd=str(values[2]),
                broker_positions=0,broker_open_orders=0,local_positions=0,local_nonfinal_orders=0,
                settlement_known=True,broker_mutations=0,writer_count=1)
            self.emit('REAL_RECONCILIATION_RESOLUTION_EVIDENCE',evidence)
            self.controller.resolve_flat_reconciliation_incidents(None,ids,account=account)
            self._reset_reconciliation_successes()
            self.controller.waiting_for_market_data=not closed
            if closed:return 'CLOSED_RECONCILED'
            return 'RECOVERY_FLAT_RECONCILED_WAITING_FOR_MARKET_DATA'
        except Exception as exc:
            self._reset_reconciliation_successes()
            return self._reconciliation_failure(exc,'ACCOUNT_ONLY_RECOVERY')

    def _pending_entry_instrument(self):
        if not self.pending or not self.pending[0].done() or self.pending[2]!='ENTRY':return None
        try:
            proposed=parse_alpha_decision(json.dumps(self.pending[0].result()['decision']))
            return proposed.open.instrument_id if proposed.action=='OPEN' else None
        except Exception:return None  # Existing _apply validates and records malformed decisions.

    def _record_cash_variance(self):
        # Best-effort evidence only; it can never weaken or replace a reconciliation check.
        try:
            evidence=getattr(self.source,'last_cash_variance',None)
            if not evidence:return
            import hashlib,json
            key='cash-variance-'+hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()[:24]
            if key==getattr(self,'_cash_variance_key',None):return
            self._cash_variance_key=key
            self.emit('REAL_CASH_VARIANCE_ACCEPTED',dict(evidence),key=key)
        except Exception:pass

    def step(self):
        now=self.clock();state=self.controller.state
        self._record_cash_variance()
        session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        inside=bool(session and session.contains(now))
        self.controller.lease();self._authority_guard(now)
        if inside and any(self.controller.state.get('latches',{}).values()):
            failure=self._cancel_latched_buys()
            if failure:return failure
        pending_orders=False;pending_reason=None
        # Known orders are only queried/reconciled; a consumed permit is never replayed.
        for oid,order in tuple(self.controller.state['orders'].items()):
            if order['final']:continue
            if order['phase']=='RESERVED':
                pending_reason=pending_reason or self._incident('REAL_UNDISPATCHED_RESERVATION_REQUIRES_REVIEW',intent_id=oid);pending_orders=True;continue
            intent=OrderIntent.model_validate(order['intent']);symbol=state['instruments'][intent.instrument_id]['ticker']
            try:body=self.gateway.transport.detail(intent.client_order_id)
            except Exception:
                pending_reason=pending_reason or self._incident('REAL_ORDER_TRANSPORT_UNCERTAIN',intent_id=oid);pending_orders=True;continue
            rows=body.get('orders',[]) if isinstance(body,dict) else []
            if len(rows)!=1 or not rows[0].get('order_id'):
                pending_reason=pending_reason or self._incident('REAL_RECOVERY_ORDER_UNCERTAIN',intent_id=oid);pending_orders=True;continue
            try:result=self.gateway.reconcile_order(intent,symbol,rows[0]['order_id'],refresh=False,body=body)
            except Exception:
                pending_reason=pending_reason or self._incident('REAL_RECOVERY_ORDER_DETAIL_INVALID',intent_id=oid);pending_orders=True;continue
            if result is None or not result.get('terminal',False):
                if not inside:
                    pending_reason=pending_reason or self._incident('REAL_UNRESOLVED_ORDER_OUTSIDE_SESSION',intent_id=oid);pending_orders=True;continue
                try:
                    if intent.side=='BUY' and intent.instrument_id in self.controller.state['positions']:
                        packet=self.source.exit_packet(self.controller.state,intent.instrument_id)
                        self.gate.check_exit_observation(packet,self.clock(),self.controller.state)
                        p=self.controller.state['positions'][intent.instrument_id]
                        if Decimal(p['protected_quantity'])!=Decimal(p['quantity']):
                            self.controller.send(Protection(instrument_id=intent.instrument_id,quantity=p['quantity'],
                                stop_usd=intent.protection.initial_stop_usd,evidence_id='real-recovered-protection-'+oid))
                except Exception:
                    self._incident('REAL_PARTIAL_ORDER_RECONCILIATION_FAILED',intent_id=oid)
                if self.clock()>=intent.deadline:
                    try:
                        if not getattr(self.source,'symbols',()):self.source.symbols=(symbol,)
                        cancel_packet=self.source.refresh(self.controller.state)
                        self.gate.check_recovery(cancel_packet,self.clock())
                        self.gateway.transport.cancel_expired(intent,self.controller,self.gate,cancel_packet)
                    except Exception:self._incident('REAL_CANCEL_STATE_UNCERTAIN',intent_id=oid)
                pending_orders=True
        if not inside:
            result=self._post_close(pending_orders,pending_reason,state)
            # After the close nothing can trade. A day that cannot reconcile is retried for a while (broker
            # records can lag), then the child stops with the incident journaled for the next preparation,
            # instead of living on and blocking the next day's start (2026-10-15).
            if result in ('CLOSED_RECONCILED','CLOSED_RECONCILED_WITH_INCIDENTS'):return result
            self.post_close_failures=getattr(self,'post_close_failures',0)+1
            first=self.__dict__.setdefault('post_close_first_failure',self.clock())
            done=(self.post_close_failures>=POST_CLOSE_ATTEMPTS
                and self.clock()-first>=POST_CLOSE_RETRY_WINDOW)
            return 'CLOSED_UNRECONCILED' if done else result
        if recoverable_flat_incident(self.controller.state):return self._recover_flat_account_incident()
        # Never skips monitoring: a failure here is journaled and the stops are still checked below.
        if self.controller.state['positions']:self._ensure_protection(session)
        safety_exits=self._monitor_owned_safety(session)
        if safety_exits:return 'SAFETY_EXITS_SUBMITTED'
        # Protection-only (carried positions without today's certificate): stops, deadlines and recorded
        # exits only; no reviews, no entries, no market gate.
        if getattr(self.controller,'protection_only',False):return 'PROTECTION_MONITORING'
        return self._in_session(session,pending_orders,pending_reason)

    def _certificate_current(self):
        check=getattr(self.gate,'offline_receipt',None)
        if check is None:return True
        try:check(self.gate.root,self.clock());return True
        except Exception:return False

    def _ensure_protection(self,session):
        """Carried positions are always watched. Without a current session certificate (preparation failed,
        or a recovery child on yesterday's certificate) the runner runs protection-only, and opens today's
        ledger session itself from held-instrument packets so exits are admissible."""
        if not getattr(self.controller,'protection_only',False) and not self._certificate_current():
            self.controller.protection_only=True
            self.emit('REAL_PROTECTION_ONLY',{'reason':'NO_CURRENT_SESSION_CERTIFICATE','session':session.label.isoformat()},
                key='real-protection-only-'+session.label.isoformat())
        state=self.controller.state
        if getattr(self.controller,'protection_only',False) and state['session']['label']<session.label.isoformat():
            try:
                packets=[]
                for iid in tuple(state['positions']):
                    packet=self.source.exit_packet(state,iid)
                    self.gate.check_exit_observation(packet,self.clock(),state)
                    packets.append(packet)
                self.controller.open_session_from_exit_packets(packets,session)
            except Exception as exc:
                return self._incident('REAL_PROTECTION_SESSION_OPEN_FAILED',
                    failure_subcode=_reconciliation_failure_subcode(exc,'PROTECTION_SESSION_OPEN'))
        return None

    def _post_close(self,pending_orders,pending_reason,state):
        # A rate-limited read is retried on a later cycle, not journaled as an incident (owner-approved 2026-10-19).
        try:account=self.source.account_observation()
        except ReadRateLimited:return 'REAL_POST_CLOSE_READ_DEFERRED'
        except Exception:return self._incident('REAL_POST_CLOSE_ACCOUNT_READ_FAILED')
        try:
            self._post_close_exact(account)
        except ReadRateLimited:return 'REAL_POST_CLOSE_READ_DEFERRED'
        except ValueError as exc:return self._incident(str(exc))
        except (KeyError,TypeError):return self._incident('REAL_POST_CLOSE_ACCOUNT_SHAPE_INVALID')
        if pending_orders:return pending_reason or 'REAL_UNRESOLVED_ORDER_OUTSIDE_SESSION'
        if recoverable_flat_incident(self.controller.state):return self._recover_flat_account_incident(closed=True)
        carrying=bool(self.controller.state['positions'])
        try:
            if carrying:self.controller.refresh_carry_recovery({**account,'evidence_id':uuid4().hex})
            else:self.controller.refresh_flat_recovery({**account,'evidence_id':uuid4().hex})
        except Exception as exc:
            return self._incident('REAL_POST_CLOSE_RECONCILIATION_FAILED',failure_subcode=_reconciliation_failure_subcode(exc,'POST_CLOSE_RECONCILE'))
        # The daily-loss latch resets at the next session; it must not keep the closed day's child alive.
        if not clean_carry(self.controller.state):
            # Exact cash/position reconciliation passed; only journaled incidents/latches remain.
            return 'CLOSED_RECONCILED_WITH_INCIDENTS' if self.controller.state['reconciliation']['ready'] else 'CLOSED_RECONCILIATION_BLOCKED'
        try:
            previous=self.last('REAL_ECONOMIC_FEEDBACK')
            if previous is None or previous.get('session')!=state['session']['label']:
                from .real_feedback import economic_feedback
                self.emit('REAL_ECONOMIC_FEEDBACK',dict(session=state['session']['label'],
                    summaries=economic_feedback(self.controller.state,self.store,self.clock())),
                    key='real-economic-feedback-'+self.controller.account_id+'-'+state['session']['label'])
        except Exception:
            self.emit('REAL_ECONOMIC_FEEDBACK_UNAVAILABLE',{'reason':'READ_ONLY_SUMMARY_FAILED'})
        return 'CLOSED_RECONCILED'

    def _in_session(self,session,pending_orders,pending_reason):
        if pending_orders:return pending_reason or 'ORDER_PENDING'
        if self._record_completed_abstention():return 'ASTRA_ABSTAIN_RECORDED'
        if not session.permits_entry(self.clock()) and not self.controller.state['positions']:
            try:
                account={**self.source.account_observation(),'evidence_id':uuid4().hex}
                self.controller.refresh_flat_recovery(account)
            except Exception as exc:return self._reconciliation_failure(exc,'ENTRY_WINDOW_CLOSED_ACCOUNT')
            return 'ENTRY_WINDOW_CLOSED_RECONCILED'
        if getattr(self.controller,'waiting_for_market_data',False):return self._wait_for_market_data()
        if not self.controller.state['positions'] and getattr(self.controller,'recovery_only',False):
            # A recovery child has no renewed entry authority. Current account
            # verification remains required, independently of candidate quotes.
            stage='ACCOUNT_OBSERVATION'
            try:
                stage='FLAT_RECOVERY_GATE'
                account,_=self.gate.read_checked(
                    lambda:{**self.source.account_observation(),'evidence_id':uuid4().hex},
                    self.clock,recovery=True,flat_state=self.controller.state)
                stage='FLAT_RECOVERY_RECONCILE'
                self.controller.refresh_flat_recovery(account)
            except Exception as exc:
                return self._incident('REAL_RECOVERY_RECONCILIATION_FAILED',failure_stage=stage,
                    failure_subcode=_reconciliation_failure_subcode(exc,stage))
            if clean_flat(self.controller.state) and hasattr(self,'prepare_waiting_market'):
                self.controller.waiting_for_market_data=True
                return self._wait_for_market_data()
            return 'RECOVERY_FLAT_RECONCILED'
        return self._advance_packet(None,session)

    def _advance_packet(self,packet,session,*,prepared=False):
        # A just-certified discovery packet proceeds immediately, with current-time gates.
        def failed(exc,stage):
            evidence=getattr(self.source,'last_quote_refresh_evidence',None)
            if evidence:self.emit('REAL_QUOTE_REFRESH_EVIDENCE',{'observations':evidence})
            if prepared and market_unavailable(exc):
                self.controller.waiting_for_market_data=True
                self.emit('REAL_MARKET_DATA_WAIT',{'reason':str(exc),'stage':stage})
                return 'WAITING_FOR_MARKET_DATA'
            return self._market_failure_or_incident(exc,stage)
        held_retry=getattr(self,'held_read_retry_at',None)
        if held_retry is not None and self.clock()<held_retry and self.controller.state['positions']:
            return 'HELD_READ_DEFERRED'
        try:
            focused=self._pending_entry_instrument()
            def acquire():
                # A packet prepared moments ago in this same cycle is reused rather
                # than re-fetched (each fetch costs rate-limited market reads). It
                # still passes the full authority audit and current-time freshness
                # validation inside read_checked, so a stale packet is rejected.
                if packet is not None:return packet
                return (self.source.refresh_instrument(self.controller.state,focused)
                    if focused and hasattr(self.source,'refresh_instrument') else self.source.refresh(self.controller.state))
            packet,checkpoint=self.gate.read_checked(acquire,
                self.clock,recovery=getattr(self.controller,'recovery_only',False))
        except Exception as exc:return failed(exc,'GATE_CHECK')
        try:
            if not self._refresh_or_wait(packet):return 'WAITING_FOR_MARKET_DATA'
        except Exception as exc:return self._reconciliation_failure(exc,'LEDGER_RECONCILE')
        try:self._record_reconciliation_success(packet,checkpoint)
        except Exception as exc:return self._reconciliation_failure(exc,'INCIDENT_RESOLUTION')
        state=self.controller.state;quotes={q.instrument_id:q for q in packet.context.instruments}
        for iid,p in state['positions'].items():
            plan=self.plan(state['orders'][p['originating_orders'][0]]['intent']['plan_id'])
            carried=plan.latest_exit_at<session.opens_at
            # Held from a previous session: reviewed once from the first quote of each new session.
            opens=self.__dict__.setdefault('open_reviews',set())
            overnight=(plan.entry_expires_at<session.opens_at and (plan.plan_id,session.label) not in opens) or plan.next_review_at<session.opens_at
            if Decimal(p['protected_quantity'])!=Decimal(p['quantity']):
                self.controller.send(Protection(instrument_id=iid,quantity=p['quantity'],stop_usd=plan.active_stop_usd,evidence_id=uuid4().hex))
            blocked_until=self.position_call_blocked_until.get(plan.plan_id)
            # The model chose the selling point: reaching its target asks it again (hold for more, raise the
            # stop, or close) instead of selling automatically. Once per (plan, target): HOLD/MODIFY bump the
            # version, so keying on the version would buy a paid review every cycle while the bid sits above it.
            quote=quotes.get(iid);targets=self.__dict__.setdefault('target_reviews',set())
            at_target=bool(plan.target_usd is not None and quote is not None and quote.bid_usd>=plan.target_usd
                and (plan.plan_id,str(plan.target_usd)) not in targets)
            # A latch (loss halt or incident) asks for a fresh evaluation of every holding, once per latch state.
            active=tuple(sorted(k for k,v in state['latches'].items() if v))
            latch_reviews=self.__dict__.setdefault('latch_reviews',set())
            latch_review=bool(active) and (plan.plan_id,active,session.label) not in latch_reviews
            # Before the close, a multi-day plan whose remaining upside no longer qualifies is re-evaluated.
            from .carry import qualifies_for_carry
            close_reviews=self.__dict__.setdefault('close_reviews',set())
            close_check=bool(plan_carries(plan,session) and quote is not None
                and self.clock()>=session.liquidation_at-timedelta(minutes=25)
                and not qualifies_for_carry(quote.bid_usd,plan.active_stop_usd,plan.target_usd)
                and (plan.plan_id,session.label) not in close_reviews)
            review_due=carried or overnight or at_target or latch_review or close_check or self.clock()>=plan.next_review_at
            if quote is not None:self._track_excursion(plan,quote.bid_usd)
            if self.pending is None and review_due and (blocked_until is None or self.clock()>=blocked_until):
                if at_target:targets.add((plan.plan_id,str(plan.target_usd)))
                if overnight:opens.add((plan.plan_id,session.label))
                if latch_review:latch_reviews.add((plan.plan_id,active,session.label))
                if close_check:close_reviews.add((plan.plan_id,session.label))
                self._start(self.context(packet),'POSITION',plan.plan_id)
        if self.pending:
            self._apply(packet);return 'ASTRA_PENDING_OR_APPLIED'
        if any(state['latches'].values()):return 'SAFETY_LATCHED'
        if getattr(self.controller,'recovery_only',False):return 'RECOVERY_MONITORING'
        if session is None or not session.permits_entry(self.clock()):return 'MONITORING'
        key,trigger=self._entry_slot(session,packet)
        if key is None:return 'MONITORING'
        # A rate-limited entry search waits out Webull's retry window before searching again (owner approval
        # 2026-10-19: "rate-limited reads are retried instead of logged as incidents"). Held positions were
        # already gated and reconciled above in this cycle, so skipping the search never skips their monitoring.
        entry_retry=getattr(self,'entry_read_retry_at',None)
        if entry_retry is not None and self.clock()<entry_retry:return 'ENTRY_READ_DEFERRED'
        if not prepared:
            try:
                self.source.discover()
                packet,_=self.gate.read_checked(lambda:self.source.refresh(self.controller.state),self.clock)
            except ReadRateLimited as exc:
                self.entry_read_retry_at=self.clock()+timedelta(seconds=max(300,exc.retry_after))   # entry searches are never urgent
                self.emit('REAL_MARKET_DATA_WAIT',{'reason':str(exc),'stage':'ENTRY_MARKET_REFRESH',
                    'endpoint':exc.path,'retry_at':self.entry_read_retry_at.isoformat()})
                return 'ENTRY_READ_DEFERRED'
            except Exception as exc:return failed(exc,'ENTRY_MARKET_REFRESH')
            try:
                if not self._refresh_or_wait(packet):return 'WAITING_FOR_MARKET_DATA'
            except Exception as exc:return self._reconciliation_failure(exc,'LEDGER_RECONCILE')
        # A prepared packet was gated and reconciled at this method's entry.
        # Repeating those operations on the identical packet only ages it.
        key,trigger=self._entry_slot(session,packet)
        if key is None:return 'MONITORING'
        context=self.context(packet)
        self.emit('REAL_PORTFOLIO_SLOT',dict(session=session.label.isoformat(),context_key=key,
            allocation='MATERIAL_CONTEXT_WITHIN_EXISTING_ATOMIC_CALL_BUDGET',**trigger),key=key)
        self._start(context,'ENTRY')
        return 'PORTFOLIO_EVALUATION'
