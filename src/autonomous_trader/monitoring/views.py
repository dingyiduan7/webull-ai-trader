"""Read-only derived views. No accounting writes and no trading/secret imports."""
from ..runtime_storage import data_root
import json
from datetime import datetime, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo
from pathlib import Path
from ..durable.ledger import LocalDatabase
from ..durable.projection import digest
from ..risk import POLICY
from ..economic_policy import effective_policy, aggregate_risk_limit, daily_downside_budget
from ..mvp.config import reviewed_calendar
from .store import MODEL_EVENT_VALUE_KEYS
from .live_panel import read_evidence,project_panel

Z=D(0)
NY=ZoneInfo('America/New_York')
def pct(n,d):return n/d*100 if d>0 else None

def bounded_series(rows,limit=600):
    """Bound dashboard payload growth while preserving the full chart time span."""
    if limit<2:raise ValueError('series limit must preserve endpoints')
    if len(rows)<=limit:return rows
    last=len(rows)-1
    indexes=[i*last//(limit-1) for i in range(limit)]
    return [rows[i] for i in indexes]

def decision_index(airows):
    """Index only legacy decisions carrying an internal id; null results are valid evidence."""
    result={}
    for call in airows:
        decision=call.get('decision') if isinstance(call,dict) else None
        if isinstance(decision,dict) and decision.get('decision_id'):
            result[decision['decision_id']]=decision
    return result

def trades_from_state(s,decisions,alpha_plans=None):
    alpha_plans=alpha_plans or {}
    lots=[];trades=[]
    for x in sorted(s['executions'].values(),key=lambda x:x['sequence']):
        intent=s['orders'][x['intent_id']]['intent'];q=D(x['quantity']);price=D(x['price_usd']);fee=D(x['fee_usd'])
        if not q:continue
        if intent['side']=='BUY':
            lots.append(dict(q=q,original=q,price=price,fee=fee,time=x['executed_at'],intent=intent));continue
        remaining=q
        for lot in lots:
            if lot['intent']['instrument_id']!=intent['instrument_id'] or lot['intent']['episode_id']!=intent['episode_id']:continue
            used=min(remaining,lot['q'])
            if not used:continue
            gross=used*(price-lot['price']);cost=used/lot['original']*lot['fee']+used/q*fee
            did=lot['intent']['plan_id'];decision=decisions.get(did,{});alpha=alpha_plans.get(did,{})
            trades.append(dict(ticker=s['instruments'][intent['instrument_id']]['ticker'],quantity=used,entry_time=lot['time'],entry_price=lot['price'],exit_time=x['executed_at'],exit_price=price,
                gross=gross,fees=cost,net=gross-cost,return_pct=pct(gross-cost,used*lot['price']),decision_id=did,
                strategy=alpha.get('strategy_label'),thesis=alpha.get('thesis'),strategy_version=decision.get('strategy_version'),origin='M2 effective FIFO executions'))
            remaining-=used;lot['q']-=used
            if not remaining:break
    return trades

def historic_trade(root):
    base=Path(root)/'tests/fixtures/webull_m3b/20260911T193156104102Z'
    buy=json.loads((base/'buy_confirmed_detail.json').read_text())['body']['orders'][0]
    sell=json.loads((base/'sell_detail_0.json').read_text())['body']['orders'][0]
    q=D(buy['filled_quantity']);entry=D(buy['filled_price']);exit=D(sell['filled_price'])
    fee=sum((D(f['actual_value']) for r in (buy,sell) for f in r['fees']),Z);gross=q*(exit-entry)
    return dict(ticker='AAPL',quantity=q,entry_time=buy['filled_time_at'],entry_price=entry,exit_time=sell['filled_time_at'],exit_price=exit,gross=gross,fees=fee,net=gross-fee,return_pct=pct(gross-fee,q*entry),decision_id=None,strategy_version=None,origin='Historical M3B certification (included in opening statement)')

def historic_orders(root):
    base=Path(root)/'tests/fixtures/webull_m3b/20260911T193156104102Z'
    result=[]
    for name in ('prior_detail.json','buy_confirmed_detail.json','sell_detail_0.json'):
        row=json.loads((base/name).read_text())['body']['orders'][0]
        result.append(dict(ticker=row['symbol'],side=row['side'],quantity=row['total_quantity'],limit=row['limit_price'],status=row['status'],
            submitted=row.get('place_time_at'),filled=row['filled_quantity'],fill_price=row.get('filled_price') if D(row['filled_quantity']) else None,
            decision_id=None,origin='Historical M3B certification'))
    return result

def account_view(s,day):
    cash=s['cash'];equity=D(cash['liquidation_equity_usd']);unrealized=D(cash['unrealized_usd']);day_realized=D(s['daily_realized'].get(day,'0'))
    current_day=s['session']['label']==day
    daily=equity-D(s['day_start_equity']) if current_day else (Z if not s['positions'] else None)
    return dict(equity=equity,settled=D(cash['settled_usd']),unsettled=D(cash['unsettled_usd']),reserved=D(cash['reserved_usd']),
        eligible_cash=max(Z,D(cash['settled_usd'])-D(cash['reserved_usd'])),market_value=sum((D(p['exposure_usd']) for p in s['positions'].values()),Z),
        daily_realized=day_realized,daily_unrealized=None if daily is None else daily-day_realized,daily_total=daily,
        unrealized=unrealized,experiment_pnl=equity-D(s['segments'][0]['reference_usd']),fees=D(cash['fees_usd']),ledger_session=s['session']['label'])

def risk_view(s):
    policy=effective_policy(s)
    eq=D(s['cash']['liquidation_equity_usd']);basis=min(eq,D(s['day_start_equity']));exposure={}
    for k,p in s['positions'].items():exposure[k]=D(p['exposure_usd'])
    for r in s['reservations'].values():exposure[r['instrument_id']]=exposure.get(r['instrument_id'],Z)+D(r['exposure_usd'])
    openrisk=sum((D(p['open_risk_usd']) for p in s['positions'].values()),Z)+sum((D(r['risk_usd']) for r in s['reservations'].values()),Z)
    day=D(s['day_start_equity']);high=D(s['segments'][-1]['high_water_usd']);historic=D(s['historic_high_water'])
    downside_active=bool(s.get('economic_policy',{}).get('daily_downside_budget'))
    effective_open_limit=aggregate_risk_limit(s)
    effective_open_fraction=effective_open_limit/basis if basis>0 else Z
    vals=[('Account exposure',sum(exposure.values(),Z),eq,policy.account_exposure),('Maximum stock exposure',max(exposure.values(),default=Z),eq,policy.stock_exposure),
        ('Aggregate planned downside (effective)',openrisk,basis,effective_open_fraction),('Daily loss',max(Z,day-eq,-D(s['daily_realized'].get(s['session']['label'],'0'))),day,policy.daily_loss),
        ('Segment drawdown',max(Z,high-eq),high,policy.drawdown),('Permanent experiment drawdown',max(Z,historic-eq),historic,policy.experiment_death)]
    halted=any(s['latches'].values()) or bool(s['risk_breaches'])
    status='HALTED' if halted else ('WARNING' if not s['reconciliation']['ready'] else 'NORMAL')
    return dict(status=status,latches=s['latches'],breaches=s['risk_breaches'],rows=[dict(name=n,amount=a,percent=pct(a,b),limit_percent=l*100) for n,a,b,l in vals],
        planned_trade_limit_percent=policy.trade_risk*100,aggregate_preference_ceiling_percent=policy.open_risk*100,
        daily_downside_active=downside_active,daily_downside_limit_usd=daily_downside_budget(s) if downside_active else None,
        remaining_daily_downside_usd=max(Z,effective_open_limit-openrisk),
        position_count=len(s['positions']),maximum_positions=policy.maximum_positions,
        effective_policy=s.get('economic_policy'),policy_hash=s['policy_hash'],
        halt_release_history=[{'at':p['effective_at'],'revision':p['revision'],'releases':p['decision']['proposal']['release_halts'],
            'reason':p['decision']['proposal']['reason']} for p in s.get('policy_history',())
            if p.get('decision',{}).get('proposal',{}).get('release_halts')])


def top_runner_view(discovered,now,positions=(),pipeline=()):
    active={x.get('symbol') for x in pipeline if x.get('status') in ('PREPARING','RUNNING','RESULT_READY','EXECUTING')}
    held={x.get('ticker') for x in positions}
    rows=[]
    discovered=discovered or {}
    for row in discovered.get('screen_runners',())[:10]:
        try:
            screen_age=(now-datetime.fromisoformat(row['screen_at'])).total_seconds()
            if screen_age<0:continue
            rows.append({**row,'screen_age_seconds':round(screen_age,1),
                'fresh':screen_age<=120,'held':row['symbol'] in held,
                'under_evaluation':row['symbol'] in active})
        except (KeyError,TypeError,ValueError):continue
    day_gainers=[]
    for row in discovered.get('day_gainers',())[:10]:
        try:
            screen_age=(now-datetime.fromisoformat(row['screen_at'])).total_seconds()
            if screen_age<0:continue
            day_gainers.append({**row,'screen_age_seconds':round(screen_age,1),
                'fresh':screen_age<=120,'held':row['symbol'] in held,
                'under_evaluation':row['symbol'] in active})
        except (KeyError,TypeError,ValueError):continue
    unavailable=discovered.get('unsupported_rank_sources',())
    reason=('Short-window Webull screens unavailable' if len(unavailable)>=2 else
        'No positive exchange-listed short-window observations' if not rows else
        f'Only {len(rows)} distinct positive exchange-listed short-window observations' if len(rows)<10 else None)
    return {'coverage':'Webull exchange-listed short-window screens; receipt-time freshness only; read-only',
        'day_gainer_coverage':'Webull GAINERS_LOSERS · DAY_1 · DESC; receipt-time freshness only; read-only',
        'observed_rows':discovered.get('observed_rows',0),
        'eligible_symbols':discovered.get('eligible_symbols',0),'observed_count':len(rows),
        'reason':reason,'rows':rows,'day_gainers':day_gainers,
        'day_gainer_reason':None if day_gainers else 'No eligible Webull DAY_1 gainers in the latest observed scan'}

def overlay_real_runtime(result,*,expected_build=None,expected_session=None,receipt_valid=False,supervisor=None,now=None):
    """Show REAL as active only from a fresh supervised runtime heartbeat."""
    system=result.get('system',{})
    now=now or datetime.now(timezone.utc)
    if (system.get('mode')=='REAL' and system.get('status') in
            ('RUNNING','INCIDENT','RECOVERY_RECONCILED','RECOVERY_UNRESOLVED')
            and system.get('ledger_schema')=='real_trader' and system.get('heartbeat')):
        rec=(result.get('account') or {}).get('reconciliation') or {}
        latches=(result.get('risk') or {}).get('latches',{})
        try:
            valid_until=datetime.fromisoformat(rec['valid_until'])
            reconciliation_current=valid_until.tzinfo is not None and now<=valid_until
        except (KeyError,TypeError,ValueError):reconciliation_current=False
        authority_bound=(receipt_valid is True and expected_build is not None and expected_session is not None
            and system.get('authority_build_sha256')==expected_build
            and system.get('market_session_label')==expected_session)
        supervisor_bound=(isinstance(supervisor,dict) and supervisor.get('status')=='HEALTHY'
            and supervisor.get('build')==expected_build
            and supervisor.get('authority_session')==expected_session
            and supervisor.get('account_binding')==system.get('account_id')
            and supervisor.get('recovery_only') is (system.get('recovery_only') is True))
        verified=(authority_bound and supervisor_bound and system.get('status')=='RUNNING'
            and system.get('reconciliation_ready') is True and bool(rec.get('ready')) and reconciliation_current
            and system.get('unresolved') is False and not any(latches.values())
            and supervisor.get('unresolved') is False)
        recovery=system.get('recovery_only') is True
        recovery_verified=bool(recovery and authority_bound and supervisor_bound)
        result['real_readiness'].update(production_enabled=bool(verified and not recovery),
            status='REAL_RECOVERY_ACTIVE' if recovery_verified else
                'REAL_SUPERVISED_ACTIVE' if verified else 'REAL_RUNTIME_EVIDENCE_INVALID',
            authority_blocker=('REAL_RECOVERY_UNRESOLVED' if recovery_verified and system.get('unresolved') else
                'REAL_RECOVERY_REDUCE_ONLY') if recovery_verified else
                None if verified else 'CURRENT_REAL_RUNTIME_EVIDENCE_REQUIRED',
            reconciliation_ready=bool(verified),latches=latches,
            current_evidence='REAL_LEDGER_BUILD_SESSION_RECEIPT_AND_SUPERVISOR' if verified or recovery_verified
                else 'UNVERIFIED_RUNTIME_HEARTBEAT')
    return result


class DashboardReader:
    def __init__(self,root):self.root=Path(root);self.db=LocalDatabase('postgres','mvp_reader',schema='mvp_monitor')
    def snapshot(self,now=None):
        now=now or datetime.now(timezone.utc);day=now.astimezone(NY).date().isoformat();session=reviewed_calendar().session(now.astimezone(NY).date())
        result={'at':now.isoformat(),'system':{'status':'STOPPED','postgres':'ERROR','market':'OPEN' if session and session.permits_entry(now) else ('LIQUIDATING' if session and session.contains(now) else 'CLOSED')},'account':None,'positions':[],'orders':[],'trades':[],'events':[],'curve':[],'risk':None,'brain':None,'analytics':{}}
        try:
            with self.db.connect() as c:
                c.execute('SET TRANSACTION READ ONLY')
                row=c.execute('SELECT heartbeat,payload FROM runtime WHERE id=1').fetchone()
                runtime=row[1] if row else {};result['system'].update(runtime)
                result['system']['postgres']='CONNECTED'
                if not row or (now-row[0]).total_seconds()>20:result['system'].update(status='ERROR' if row else 'STOPPED',activity='Heartbeat missing or stale; trader not confirmed alive')
                else:result['system']['heartbeat']=row[0].isoformat()
                mode=runtime.get('mode','DRY_RUN');schema=runtime.get('ledger_schema');aid=runtime.get('account_id')
                events=[dict(id=i,at=t.isoformat(),kind=k,payload=p) for i,t,k,p in c.execute("""
                    SELECT id,at,kind,CASE
                        WHEN kind IN ('UNIVERSE_DISCOVERED','EQUITY_SAMPLE','BASELINE_RESULT','BASELINE_SNAPSHOT')
                            THEN payload
                        WHEN kind IN ('ASTRA_DECISION','ALPHA_DECISION')
                            THEN jsonb_build_object('ai',jsonb_build_object(
                                'decision',payload->'ai'->'decision','result',payload->'ai'->'result'))
                        ELSE jsonb_build_object('summary',payload->'summary','blocker',payload->'blocker')
                        END AS payload
                    FROM events WHERE mode=%s ORDER BY at DESC LIMIT 5000
                    """,(mode,))]
                s=None;journal=[]
                if schema in ('mvp_sandbox','mvp_dry_run','real_trader') and aid:
                    from psycopg import sql
                    row=c.execute(sql.SQL('SELECT state,state_hash FROM {}.accounts WHERE account_id=%s').format(sql.Identifier(schema)),(aid,)).fetchone()
                    if row:
                        if digest(row[0])!=row[1]:raise ValueError('CHECKPOINT_INTEGRITY_FAILURE')
                        s=row[0]
                        journal=[r[0] for r in c.execute(sql.SQL("SELECT payload FROM {}.journal WHERE account_id=%s AND kind NOT IN ('LEASE','MARK','RECONCILE') ORDER BY sequence DESC").format(sql.Identifier(schema)),(aid,))]
                # Costs over all captured calls, not just the visible timeline window.
                # Model events can contain large prompt/context copies. The dashboard
                # only needs cost claims and decisions; decode those small fields
                # instead of every historical prompt on every refresh.
                raw_calls=list(c.execute("""
                    WITH model_values AS (
                        SELECT at,kind,payload -> CASE kind
                            WHEN 'ASTRA_SELECTION' THEN 'selection'
                            WHEN 'ALPHA_DEEP_PLAN' THEN 'deep'
                            WHEN 'SHADOW_AUDIT' THEN 'shadow'
                            ELSE 'ai' END AS value
                        FROM events WHERE kind = ANY(%s) AND mode=%s)
                    SELECT at,kind,jsonb_build_object(
                        'decision',value->'decision',
                        'result',value->'result',
                        'estimated_cost_usd',value->'estimated_cost_usd',
                        'model_work_id',value->'model_work_id',
                        'cost_authority',value->'cost_authority',
                        'call_type',value->'call_type') AS value
                    FROM model_values ORDER BY at DESC
                    """,(list(MODEL_EVENT_VALUE_KEYS),mode)))
                plan_rows=list(c.execute("SELECT payload FROM events WHERE kind IN ('ALPHA_PLAN_CREATED','ALPHA_PLAN_MODIFIED') AND mode=%s ORDER BY at",(mode,)))
                panel_evidence=read_evidence(c,now,mode)
            calls=[]
            for at,kind,payload in raw_calls:
                value=payload
                if value:calls.append((at,kind,value))
            airows=[x[2] for x in calls];decisions=decision_index(airows)
            alpha_plans={p[0]['plan_id']:p[0]['plan'] for p in plan_rows}
            cost_calls=[];claimed={}
            for row in calls:
                value=row[2];key=value.get('model_work_id')
                if not key:cost_calls.append(row)
                elif key not in claimed or value.get('cost_authority')=='BILLABLE_WORK':claimed[key]=row
            cost_calls.extend(claimed.values())
            known=sum((D(a['estimated_cost_usd']) for _,_,a in cost_calls if a.get('estimated_cost_usd') is not None),Z)
            unknown=sum(a.get('estimated_cost_usd') is None for _,_,a in cost_calls)
            today=sum((D(a['estimated_cost_usd']) for at,_,a in cost_calls if at.astimezone(NY).date().isoformat()==day and a.get('estimated_cost_usd') is not None),Z)
            latest_decision=next((a for _,k,a in calls if k in ('ASTRA_DECISION','ALPHA_DECISION','ALPHA_POSITION_REVIEW')),None)
            if latest_decision:result['brain']=latest_decision
            breakdown={}
            for _,kind,a in cost_calls:
                kind=a.get('call_type') or kind
                if a.get('estimated_cost_usd') is not None:breakdown[kind]=breakdown.get(kind,Z)+D(a['estimated_cost_usd'])
            result['api']={'total_known_cost':known,'day_known_cost':today,'unknown_cost_calls':unknown,'cost_complete':unknown==0,
                'by_call_type':breakdown}
            if s:
                result['account']=account_view(s,day);result['risk']=risk_view(s)
                rec=s['reconciliation'];result['account']['reconciliation']=rec
                if not rec.get('ready') or not rec.get('valid_until') or now>datetime.fromisoformat(rec['valid_until']):
                    if result['risk']['status']=='NORMAL':result['risk']['status']='WARNING'
                for p in s['positions'].values():
                    q=D(p['quantity']);basis=D(p['basis_usd']);mv=D(p['exposure_usd']);origin=s['orders'][p['originating_orders'][0]]['intent'];decision=decisions.get(origin['plan_id'],{});alpha=alpha_plans.get(origin['plan_id'],{})
                    result['positions'].append(dict(ticker=s['instruments'][p['instrument_id']]['ticker'],quantity=q,entry=basis/q,price=D(p['mark_usd']),market_value=mv,unrealized=mv-basis,unrealized_pct=pct(mv-basis,basis),stop=p['stop_usd'],target=alpha.get('target_usd'),strategy=alpha.get('strategy_label'),invalidation=(decision.get('entry_plan') or {}).get('invalidation'),risk=p['open_risk_usd']))
                for oid,o in s['orders'].items():
                    i=o['intent'];xs=[x for x in s['executions'].values() if x['intent_id']==oid];qty=sum((D(x['quantity']) for x in xs),Z)
                    submitted=next((e['logical_at'] for e in reversed(journal) if e['command']['kind']=='CONSUME' and s['permits'].get(e['command']['nonce'],{}).get('intent_id')==oid),None)
                    result['orders'].append(dict(ticker=s['instruments'][i['instrument_id']]['ticker'],side=i['side'],quantity=i['quantity'],limit=i['limit_price_usd'],status=o['phase'],submitted=submitted,filled=qty,fill_price=sum((D(x['quantity'])*D(x['price_usd']) for x in xs),Z)/qty if qty else None,decision_id=i['plan_id']))
                result['trades']=trades_from_state(s,decisions,alpha_plans)
                if s['config'].get('opening_statement'):
                    result['trades'].insert(0,historic_trade(data_root(self.root)))
                    result['orders']=historic_orders(data_root(self.root))+result['orders']
                for e in journal:
                    k=e['command']['kind']
                    if k not in ('LEASE','MARK','RECONCILE'):
                        result['events'].append(dict(at=e['logical_at'],kind='LEDGER_'+k,summary=ledger_summary(e)))
                a=result['account'];wins=[t['net'] for t in result['trades'] if t['net']>0];losses=[t['net'] for t in result['trades'] if t['net']<0]
                n=len(result['trades']);result['analytics']=dict(wins=len(wins),losses=len(losses),breakeven=n-len(wins)-len(losses),win_rate=pct(D(len(wins)),D(n)),
                    average_win=sum(wins,Z)/len(wins) if wins else None,average_loss=sum(losses,Z)/len(losses) if losses else None,
                    profit_factor=sum(wins,Z)/-sum(losses,Z) if losses else None,total_fees=a['fees'],trading_pnl=a['experiment_pnl'],
                    experimental_net_known_cost=a['experiment_pnl']-known,daily_pnl=s['daily_realized'])
                result['economics']={'trading_pnl_usd':a['experiment_pnl'],'broker_fees_usd':a['fees'],
                    'model_cost_usd':known,'net_after_known_cost_usd':a['experiment_pnl']-known,
                    'operating_cost_complete':False,'baseline_is_counterfactual_only':True}
            discovered=next((e['payload'] for e in events if e['kind']=='UNIVERSE_DISCOVERED'),None)
            result['top_runners']=top_runner_view(discovered,now,result['positions'],
                runtime.get('pipeline',{}).get('candidates',()))
            for e in events:
                if e['kind']=='EQUITY_SAMPLE':result['curve'].append(dict(at=e['at'],**e['payload']))
                elif e['kind'] not in ('BROKER_REFRESH','HEARTBEAT'):result['events'].append(dict(at=e['at'],kind=e['kind'],summary=event_summary(e)))
            samples=[x for x in result['curve'] if x.get('gross_exposure_usd') is not None]
            if samples:
                exposures=[D(x['gross_exposure_usd']) for x in samples];equities=[D(x['equity']) for x in samples]
                result['capital_utilization']={'maximum_gross_exposure_usd':max(exposures),'average_gross_exposure_usd':sum(exposures,Z)/len(exposures),
                    'maximum_equity_fraction_percent':max((pct(x,y) for x,y in zip(exposures,equities) if y>0),default=None),
                    'sampled_flat_percent':pct(D(sum(x==0 for x in exposures)),D(len(exposures)))}
            baseline=next((e['payload'] for e in events if e['kind']=='BASELINE_RESULT'),None) or next((e['payload'] for e in events if e['kind']=='BASELINE_SNAPSHOT'),None)
            if baseline:result['baselines']=baseline
            result['curve'].reverse();result['curve']=bounded_series(result['curve'])
            result['events']=sorted(result['events'],key=lambda e:datetime.fromisoformat(e['at']),reverse=True)[:150]
            result['live_panel']=project_panel(result,s,journal,panel_evidence,now)
            result['orders']=sorted(result['orders'],key=lambda o:o['submitted'] or '')[-100:];result['trades']=result['trades'][-100:]
        except Exception:
            result['system'].update(status='ERROR',postgres='UNAVAILABLE_OR_READ_FAILED',activity='Cannot establish current ledger state; values unavailable')
            result.update(account=None,risk=None,positions=[],orders=[],trades=[])
            result['live_panel']={'status':'BLOCKED','blockers':['MONITORING_SNAPSHOT_UNAVAILABLE']}
        try:
            # Only display fixed startup error classification, never arbitrary log bytes.
            error_log=data_root(self.root)/'.local/mvp/startup-errors.log'
            result['startup_errors']=[{'at':datetime.fromtimestamp(error_log.stat().st_mtime,timezone.utc).isoformat(),'kind':'STARTUP_ERROR','summary':'A prior startup failed; local startup-errors.log contains the diagnostic category.'}] if error_log.exists() and error_log.stat().st_size else []
            from ..real_readiness import dashboard_readiness
            result['real_readiness']=dashboard_readiness(self.root,now)
            from ..real_trading import RealGate
            label=session.label.isoformat() if session else None
            receipt_valid=False;receipt=None
            try:
                recovery=result.get('system',{}).get('recovery_only') is True
                receipt=RealGate.offline_receipt(self.root,now,recovery=recovery);receipt_valid=True
            except ValueError:pass
            supervisor=None
            try:
                supervisor=json.loads((data_root(self.root)/'.local/real-supervisor/supervisor-status.json').read_text(encoding='utf-8'))
                at=datetime.fromisoformat(supervisor['at'])
                if at.tzinfo is None or not 0<=(now-at).total_seconds()<=20:supervisor=None
            except (OSError,ValueError,KeyError,TypeError):pass
            authority_session=receipt['market_session_label'] if receipt else label
            return overlay_real_runtime(result,expected_build=cached_build_digest(self.root),expected_session=authority_session,
                receipt_valid=receipt_valid,supervisor=supervisor,now=now)
        except Exception as exc:
            # Never drop the response: report the class so the page can show the real reason.
            result['system'].update(status='ERROR',last_error=type(exc).__name__,
                activity='Dashboard overlay failed ('+type(exc).__name__+'); ledger values above may still be valid')
            result.setdefault('live_panel',{'status':'BLOCKED','blockers':['DASHBOARD_OVERLAY_FAILED:'+type(exc).__name__]})
            return result

_DIGEST_CACHE={}


def cached_build_digest(root,seconds=60):
    """build_digest hashes the whole source tree; recomputing it on every 2 s poll made snapshots slow."""
    import time
    from ..real_trading import build_digest
    key=str(root);now=time.monotonic();hit=_DIGEST_CACHE.get(key)
    if hit and now-hit[0]<seconds:return hit[1]
    value=build_digest(root);_DIGEST_CACHE[key]=(now,value);return value


def ledger_summary(e):
    c=e['command'];k=c['kind']
    if k=='ADMIT':return 'Risk APPROVED: '+c['intent']['side']+' '+c['intent']['quantity']+' '+c['intent']['instrument_id']
    if k=='EXECUTION':return 'Fill '+c['quantity']+' @ '+c['price_usd']
    if k=='REJECTED':return 'Risk/controller rejected: '+c.get('reason','')
    if k=='CONSUME':return 'Permit consumed; submission claimed (not proof of broker acceptance)'
    if k=='FINALIZE':return 'Order terminal: '+c.get('status','')
    return k

def event_summary(e):
    p=e['payload']
    if e['kind']=='ASTRA_DECISION':
        d=p['ai'].get('decision') or {};return 'Astra: '+d.get('action',p['ai'].get('result','UNKNOWN'))+' '+d.get('reasoning_summary','')
    if e['kind']=='ALPHA_DECISION':
        d=p['ai'].get('decision') or {};return 'Astra fast: '+d.get('action','UNKNOWN')+' '+d.get('reason','')
    return p.get('summary') or p.get('blocker') or e['kind']
