"""Read-only live pipeline projection. No order, model, or control handles."""
import json
from datetime import datetime,timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo
from .store import compact_performance_memory,corrected_outcomes

NY=ZoneInfo('America/New_York')
Z=D(0)

def stamp(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value

def seconds(start,end):
    if not start or not end:return None
    delta=(stamp(end)-stamp(start)).total_seconds()
    return round(delta,3) if delta>=0 else None

def read_evidence(c,now,mode):
    start=now.astimezone(NY).replace(hour=0,minute=0,second=0,microsecond=0)
    rows=c.execute("""SELECT id,at,kind,jsonb_build_object(
        'ai',jsonb_build_object('decision',payload->'ai'->'decision','model_work_id',payload->'ai'->'model_work_id',
            'latency_ms',payload->'ai'->'latency_ms'),
        'symbol',payload->'symbol','instrument_id',payload->'instrument_id','job_id',payload->'job_id',
        'decision_id',payload->'envelope'->'decision_id','latency_trace',payload->'latency_trace',
        'broker',payload->'broker','reason',payload->'reason','failure_code',payload->'failure_code',
        'call_type',payload->'call_type','category',payload->'category','id',payload->'id')
        FROM events WHERE mode=%s AND at>=%s AND at<=%s AND kind=ANY(%s) ORDER BY at""",
        (mode,start,now,['MODEL_WORK_CLAIM','ALPHA_DECISION','ALPHA_POSITION_REVIEW','ASTRA_DECISION',
            'ALPHA_ADMISSION_DROP','BROKER_RESULT','ALPHA_DEEP_PLAN_FAILED','MODEL_CALL_FAILED'])).fetchall()
    discovery=c.execute("SELECT at,payload FROM events WHERE kind='UNIVERSE_DISCOVERED' AND mode=%s AND at<=%s ORDER BY at DESC LIMIT 1",(mode,now)).fetchone()
    scan_totals=c.execute("""SELECT count(*),coalesce(sum((payload->>'unique_symbols')::bigint),0),
        coalesce(sum((payload->>'shortlisted_symbols')::bigint),0)
        FROM events WHERE kind='UNIVERSE_DISCOVERED' AND mode=%s AND at>=%s AND at<=%s""",(mode,start,now)).fetchone()
    queue=c.execute("""SELECT jsonb_object_agg(j.key,jsonb_build_object(
        'symbol',j.value->'symbol','instrument_id',j.value->'instrument_id','rank',j.value->'rank',
        'current',j.value->'current','first_at',j.value->'first_at','status',j.value->'status',
        'reason',j.value->'reason','updated_at',j.value->'updated_at','session',j.value->'session',
        'quote',j.value->'dossier'->'quotes'->-1))
        FROM (SELECT payload FROM events WHERE kind='OPPORTUNITY_QUEUE' AND mode=%s AND at<=%s ORDER BY at DESC LIMIT 1) q,
        LATERAL jsonb_each(q.payload->'jobs') j""",(mode,now)).fetchone()
    metrics=c.execute("""SELECT DISTINCT ON (payload->>'plan_id') payload || jsonb_build_object('recorded_at',at) FROM events
        WHERE kind='ALPHA_POSITION_METRICS' AND mode=%s AND at<=%s ORDER BY payload->>'plan_id',at DESC""",(mode,now)).fetchall()
    originals=c.execute("SELECT id,payload FROM events WHERE kind='ALPHA_OUTCOME' AND mode=%s AND at<=%s ORDER BY at,id",(mode,now)).fetchall()
    corrections=c.execute("SELECT payload FROM events WHERE kind='ALPHA_OUTCOME_CORRECTION' AND mode=%s AND at<=%s ORDER BY at,id",(mode,now)).fetchall()
    outcomes=corrected_outcomes(originals,[r[0] for r in corrections])
    missed=c.execute("SELECT at,payload FROM events WHERE kind='OPPORTUNITY_OUTCOME' AND mode=%s AND at<=%s ORDER BY at DESC LIMIT 1",(mode,now)).fetchone()
    # REAL pipeline evidence (2026-10-19): the REAL runner records entry decisions as PORTFOLIO_DECISION_EVIDENCE
    # and its activity as REAL_* events; the MVP discovery/decision kinds above are not emitted in REAL mode.
    real_decisions=c.execute("""SELECT id,at,jsonb_build_object('action',payload->'action','reason',payload->'stated_reason',
        'symbol',payload->'selected_symbol','instrument_id',payload->'selected_instrument_id','decision_id',payload->'decision_id',
        'candidates',jsonb_array_length(coalesce(payload->'candidates','[]'::jsonb)),'latency_ms',payload->'latency_ms',
        'snapshot',payload->'decision_snapshot_at','model',payload->'model',
        'symbols',(SELECT jsonb_agg(x->'symbol') FROM jsonb_array_elements(coalesce(payload->'candidates','[]'::jsonb)) x))
        FROM events WHERE kind='PORTFOLIO_DECISION_EVIDENCE' AND mode=%s AND at>=%s AND at<=%s ORDER BY at""",(mode,start,now)).fetchall()
    real_reviews=c.execute("""SELECT id,at,jsonb_build_object('reason',payload->'reason','plan_id',payload->'plan_id',
        'instrument_id',payload->'plan'->'instrument_id') FROM events WHERE kind='REAL_PLAN' AND mode=%s AND at>=%s AND at<=%s
        AND payload->>'original'='false' AND payload ? 'reason' ORDER BY at""",(mode,start,now)).fetchall()
    real_activity=c.execute("""SELECT max(at) FROM events WHERE mode=%s AND at>=%s AND at<=%s AND kind=ANY(%s)""",
        (mode,start,now,['REAL_PORTFOLIO_SLOT','REAL_QUOTE_REFRESH_EVIDENCE','PORTFOLIO_DECISION_EVIDENCE','REAL_DUAL_BRAIN_DECISION'])).fetchone()
    real_dual=c.execute("""SELECT at,jsonb_build_object('snapshot',payload->'context_as_of','opus',payload->'primary_action',
        'opus_instrument',payload->'primary_instrument','astra',payload->'second_action','astra_instrument',payload->'second_instrument',
        'astra_reason',payload->'second_reason','astra_error',payload->'second_error','consensus',payload->'consensus_action',
        'consensus_mode',payload->'consensus_mode') FROM events WHERE kind='REAL_DUAL_BRAIN_DECISION' AND mode=%s AND at>=%s AND at<=%s
        ORDER BY at""",(mode,start,now)).fetchall()
    real_sizing=c.execute("""SELECT payload FROM events WHERE kind='REAL_STRATEGY_ALLOCATION' AND mode=%s AND at>=%s AND at<=%s""",
        (mode,start,now)).fetchall()
    real_wait=c.execute("""SELECT at FROM events WHERE mode=%s AND kind='REAL_MARKET_DATA_WAIT' AND at>=%s AND at<=%s
        ORDER BY at DESC LIMIT 1""",(mode,start,now)).fetchone()
    return dict(rows=rows,discovery=discovery,scan_totals=scan_totals,jobs=(queue[0] or {}) if queue else {},
        metrics={r[0]['plan_id']:r[0] for r in metrics},outcomes=outcomes,missed=missed,
        real_decisions=real_decisions,real_reviews=real_reviews,real_activity=real_activity[0] if real_activity else None,
        real_wait=real_wait[0] if real_wait else None,real_dual=real_dual,real_sizing=[r[0] for r in real_sizing])


def project_panel(result,state,journal,evidence,now):
    day=now.astimezone(NY).date().isoformat();system=result['system'];account=result.get('account') or {}
    jobs=evidence['jobs'];rows=evidence['rows'];decisions=[];seen=set();claims=[];drops=[];traces={};denials=[]
    for eid,at,kind,p in rows:
        if kind=='MODEL_WORK_CLAIM':claims.append((at,p))
        reason=p.get('reason') or p.get('failure_code')
        if reason and reason.startswith('MODEL_'):denials.append((at,reason))
        if kind=='ALPHA_ADMISSION_DROP':drops.append({'at':at,'symbol':p.get('symbol'),'reason':reason})
        trace=p.get('latency_trace') or {};key=trace.get('model_started_at')
        if key:traces.setdefault(key,{}).update({k:v for k,v in trace.items() if v is not None})
        if kind not in ('ALPHA_DECISION','ALPHA_POSITION_REVIEW','ASTRA_DECISION'):continue
        ai=p.get('ai') or {};d=ai.get('decision') or {}
        if not d.get('action'):continue
        identity=ai.get('model_work_id') or p.get('job_id') or d.get('decision_id') or eid
        if identity in seen:continue
        seen.add(identity)
        opening=d.get('open') or {};plan=d.get('entry_plan') or {}
        position_id=(d.get('hold') or d.get('modify') or d.get('close') or {}).get('position_id','')
        iid=p.get('instrument_id') or opening.get('instrument_id') or position_id.removeprefix('position-')
        symbol=p.get('symbol') or opening.get('symbol') or plan.get('symbol') or (state or {}).get('instruments',{}).get(iid,{}).get('ticker')
        decisions.append({'at':at,'action':d['action'],'reason':(d.get('reason') or d.get('reasoning_summary') or '')[:500],
            'symbol':symbol,'instrument_id':iid,
            'decision_id':p.get('decision_id') or d.get('decision_id'),'started_at':key,
            'model_latency_ms':ai.get('latency_ms')})
    for eid,at,p in evidence.get('real_decisions',()):
        if eid in seen or not p.get('action'):continue
        seen.add(eid)
        decisions.append({'at':at,'action':p['action'],'reason':(p.get('reason') or '')[:500],'symbol':p.get('symbol'),
            'instrument_id':p.get('instrument_id'),'decision_id':p.get('decision_id'),'started_at':None,
            'model_latency_ms':p.get('latency_ms'),'candidates':p.get('candidates')})
    for eid,at,p in evidence.get('real_reviews',()):
        iid=p.get('instrument_id');reason=p.get('reason') or ''
        action=('CLOSE' if reason.startswith('MODEL_CLOSE') else 'HOLD')
        decisions.append({'at':at,'action':action,'reason':reason[:500],'instrument_id':iid,
            'symbol':(state or {}).get('instruments',{}).get(iid,{}).get('ticker'),'decision_id':p.get('plan_id'),
            'started_at':None,'model_latency_ms':None})
    decisions.sort(key=lambda d:stamp(d['at']))
    accepted=set();submitted={};fills={};buy_fills=set();order_times={}
    for event in journal:
        cmd=event['command'];kind=cmd['kind'];oid=cmd.get('intent_id')
        if kind=='ADMIT':oid=cmd['intent']['intent_id']
        if oid and (oid not in order_times or stamp(event['logical_at'])>stamp(order_times[oid])):order_times[oid]=event['logical_at']
        if kind=='ORDER_OBSERVATION' and cmd.get('status')=='ACKNOWLEDGED':accepted.add(oid)
        if kind=='CONSUME':
            oid=state.get('permits',{}).get(cmd['nonce'],{}).get('intent_id') if state else None
            if oid:submitted[oid]=event['logical_at']
    orders=(state or {}).get('orders',{})
    for execution in (state or {}).get('executions',{}).values():
        oid=execution['intent_id'];at=execution['executed_at']
        if oid not in fills or stamp(at)<stamp(fills[oid]):fills[oid]=at
        accepted.add(oid)
        if orders[oid]['intent']['side']=='BUY' and stamp(at).astimezone(NY).date().isoformat()==day:buy_fills.add(oid)
    entry_orders={oid:o for oid,o in orders.items() if o['intent']['side']=='BUY' and oid in submitted
        and stamp(submitted[oid]).astimezone(NY).date().isoformat()==day}
    today_orders=[]
    for oid,o in orders.items():
        at=order_times.get(oid) or submitted.get(oid)
        if not at or stamp(at).astimezone(NY).date().isoformat()!=day:continue
        i=o['intent'];today_orders.append({'symbol':state['instruments'][i['instrument_id']]['ticker'],
            'side':i['side'],'quantity':i['quantity'],'status':o['phase'],'limit':i['limit_price_usd'],
            'submitted_at':submitted.get(oid),'filled_at':fills.get(oid),'updated_at':at,
            'broker_acceptance_confirmed':oid in accepted})
    discovery_at,discovery=evidence['discovery'] or (None,{})
    opportunities=[]
    for j in sorted(jobs.values(),key=lambda j:(j.get('rank') or 9999,j.get('symbol') or '')):
        if not j.get('current'):continue
        q=j.get('quote') or {};bid=D(q['bid']) if q.get('bid') else None;ask=D(q['ask']) if q.get('ask') else None
        opportunities.append({'symbol':j['symbol'],'rank':j.get('rank'),'first_detected':j.get('first_at'),
            'move_percent':D(q['short_window_change_ratio'])*100 if q.get('short_window_change_ratio') is not None else None,
            'window_minutes':q.get('short_window_minutes'),'relative_volume':q.get('relative_volume_10d'),
            'spread_bps':(ask-bid)/bid*10000 if bid and ask else None,'quote_at':q.get('quote_at'),
            'quote_age_seconds':seconds(q.get('quote_at'),now),'status':j.get('status'),'reason':j.get('reason'),
            'historical':j.get('session')!=day or system.get('market')=='CLOSED'})
    # Ledger fills are authoritative; an API call/permit is not a broker acceptance.
    real=evidence.get('real_decisions') or ()
    if not discovery_at and real:
        discovery_at=real[-1][1];discovery={'unique_symbols':real[-1][2].get('candidates'),'shortlisted_symbols':real[-1][2].get('candidates')}
    funnel={'day':day,'latest_scan_at':discovery_at,'scanned':discovery.get('unique_symbols'),
        'shortlisted':discovery.get('shortlisted_symbols'),'scan_passes_today':evidence['scan_totals'][0],
        'symbol_observations_today':evidence['scan_totals'][1],'shortlist_observations_today':evidence['scan_totals'][2],
        'sent_to_astra':max(sum(p.get('call_type') in ('FAST_ENTRY','PORTFOLIO_ENTRY') for _,p in claims),len(real)),
        'opens':sum(d['action'] in ('OPEN','PROPOSE_ENTRY') for d in decisions),
        'rejected_opens':len(drops),'submission_attempts':len(entry_orders),
        'submitted':len(set(entry_orders)&accepted),'filled':len(buy_fills)}
    latency=[]
    for d in reversed(decisions):
        t=traces.get(d['started_at'],{});oid=next((oid for oid,o in orders.items() if
            o['intent']['plan_id']==d['decision_id'] and o['intent']['side']=='BUY'),None)
        j=next((j for j in jobs.values() if j.get('instrument_id')==d['instrument_id']),{})
        detected=t.get('hot_signal_at') or t.get('hot_promoted_at') or j.get('first_at')
        order_at=submitted.get(oid);fill_at=fills.get(oid)
        latency.append({'symbol':d['symbol'],'action':d['action'],'at':d['at'],
            'detected_at':detected,'astra_at':d['started_at'],'order_at':order_at,'fill_at':fill_at,
            'detection_to_astra_s':seconds(detected,d['started_at']),
            'astra_s':D(d['model_latency_ms'])/1000 if d['model_latency_ms'] is not None else seconds(d['started_at'],t.get('model_handled_at')),
            'decision_to_order_s':seconds(t.get('model_handled_at'),order_at),
            'order_to_fill_s':seconds(order_at,fill_at),'total_s':seconds(detected,fill_at)})
    # Plan identity is durable. ALPHA_OUTCOME is emitted after the broker fill
    # is journaled, so its timestamp can differ from the execution timestamp.
    closed_today=[t for t in result.get('trades',[]) if t.get('exit_time') and stamp(t['exit_time']).astimezone(NY).date().isoformat()==day]
    closures={ (t.get('decision_id'),t['exit_time']):t for t in closed_today }
    memory_updates=[]
    for (plan,closed),trade in closures.items():
        outcome=next((o for o in evidence['outcomes'] if o.get('plan_id')==plan and o.get('closed_at')
            and timedelta(0)<=stamp(o['closed_at'])-stamp(closed)<=timedelta(minutes=5)),None)
        memory_updates.append({'symbol':trade['ticker'],'closed_at':closed,'plan_id':plan,
            'status':'RECORDED' if outcome else 'MEMORY_UPDATE_NOT_RECORDED',
            'net':outcome.get('realized_net_after_all_known_costs_usd') if outcome else None})
    managed={plan for plan,m in evidence['metrics'].items() if m.get('recorded_at') and stamp(m['recorded_at']).astimezone(NY).date().isoformat()==day}
    funnel.update(managed=len(managed),exited=len(closures),memory_recorded=sum(x['status']=='RECORDED' for x in memory_updates))
    position_metrics=evidence['metrics']
    for position in result['positions']:
        iid=next((i for i,v in (state or {}).get('instruments',{}).items() if v['ticker']==position['ticker']),None)
        owned=(state or {}).get('positions',{}).get(iid,{})
        oid=(owned.get('originating_orders') or [None])[0]
        plan_id=orders.get(oid,{}).get('intent',{}).get('plan_id');m=position_metrics.get(plan_id,{})
        position.update(mfe=m.get('maximum_favorable_excursion_usd'),mae=m.get('maximum_adverse_excursion_usd'))
    groups=[]
    for chunk in compact_performance_memory(evidence['outcomes']):
        groups.extend(json.loads(chunk).get('groups',[]))
    latest=max(evidence['outcomes'],key=lambda p:p.get('closed_at',''),default=None)
    latest={k:latest.get(k) for k in ('symbol','strategy_label','market_regime','closed_at','exit_reason',
        'realized_net_after_all_known_costs_usd','evidence_quality','failure_origin')} if latest else None
    missed=evidence.get('missed')
    if missed and (not latest or stamp(latest['closed_at'])<missed[0]):
        latest={'symbol':missed[1].get('symbol'),'closed_at':missed[0],'evidence_quality':'UNFILLED_COUNTERFACTUAL',
            'failure_origin':missed[1].get('failure_origin'),'exit_reason':missed[1].get('execution_block'),
            'realized_net_after_all_known_costs_usd':None}
    fees=sum((D(x['fee_usd']) for x in (state or {}).get('executions',{}).values()
        if stamp(x['executed_at']).astimezone(NY).date().isoformat()==day),Z)
    after_fees=account.get('daily_realized');api=(result.get('api') or {}).get('day_known_cost')
    # daily_realized already includes fees. Never subtract broker fees twice.
    economics={'broker_fees_today':fees,'api_today':api,'realized_after_broker_fees':after_fees,
        'net_after_known_costs':after_fees-api if after_fees is not None and api is not None else None,
        'unknown_model_cost_calls':(result.get('api') or {}).get('unknown_cost_calls'),
        'api_share_of_profit_percent':api/after_fees*100 if api is not None and after_fees is not None and after_fees>0 else None,
        'cost_comparison':'NO_POSITIVE_REALIZED_PROFIT' if after_fees is not None and after_fees<=0 else 'API_COST_VS_BROKER_NET_PROFIT'}
    blocks=[];rec=account.get('reconciliation') or {};latches=(result.get('risk') or {}).get('latches',{})
    if not system.get('heartbeat') or (now-stamp(system['heartbeat'])).total_seconds()>20:blocks.append('TRADER_HEARTBEAT_STALE')
    if system.get('last_error'):blocks.append(system['last_error'])
    if system.get('postgres')!='CONNECTED':blocks.append('LEDGER_READ_UNAVAILABLE')
    blocks.extend('SAFETY_LATCH_'+k.upper() for k,v in latches.items() if v)
    if not rec.get('ready'):blocks.append('RECONCILIATION_NOT_READY')
    if not rec.get('settlement_known'):blocks.append('SETTLEMENT_UNKNOWN')
    # valid_until is quote_at+10s and lapses between decision cycles while the trader is healthy.
    # It is a blocker only when the trader is not demonstrably alive (stale heartbeat) or not ready.
    heartbeat_fresh=bool(system.get('heartbeat')) and (now-stamp(system['heartbeat'])).total_seconds()<=20
    if system.get('market')!='CLOSED' and rec.get('valid_until') and now>stamp(rec['valid_until']) and not (heartbeat_fresh and rec.get('ready')):blocks.append('RECONCILIATION_EXPIRED')
    if any(not o['final'] and o['phase'] in ('CLAIMED_UNCERTAIN','UNKNOWN','CANCEL_PENDING') for o in orders.values()):blocks.append('BROKER_ORDER_STATE_UNCERTAIN')
    mb=system.get('model_budget') or {};last_denial=denials[-1] if denials else None
    if mb.get('exhausted'):blocks.append('MODEL_MONTHLY_BUDGET_EXHAUSTED')
    elif last_denial and last_denial[1]=='MODEL_MONTHLY_BUDGET_EXHAUSTED' and not any(at>last_denial[0] for at,_ in claims) and not ((state or {}).get('economic_policy',{}).get('origin')=='OWNER_MODEL_BUDGET_CAP' and stamp(state['economic_policy']['effective_at'])>last_denial[0]):
        blocks.append('MODEL_MONTHLY_BUDGET_EXHAUSTED (last denial; no later successful request)')
    if system.get('market')=='CLOSED':blocks.insert(0,'SESSION_CLOSED')
    recent_fill=max(fills.values(),key=stamp,default=None)
    recent_exit=max((t.get('exit_time') for t in result.get('trades',[]) if t.get('exit_time')),key=stamp,default=None)
    pending=any(not o['final'] and o['phase']=='ACKNOWLEDGED' for o in orders.values())
    if blocks:status='BLOCKED'
    elif pending:status='ORDER SENT'
    elif recent_exit and seconds(recent_exit,now) is not None and seconds(recent_exit,now)<15:status='EXITED'
    elif recent_fill and seconds(recent_fill,now) is not None and seconds(recent_fill,now)<15:status='FILLED'
    elif result['positions']:status='MANAGING'
    elif (system.get('pipeline') or {}).get('in_flight',0):status='ASTRA DECIDING'
    elif discovery_at and seconds(discovery_at,now) is not None and seconds(discovery_at,now)<120:status='SCANNING'
    elif evidence.get('real_activity') and seconds(evidence['real_activity'],now) is not None and seconds(evidence['real_activity'],now)<300:
        status='SCANNING'
    elif evidence.get('real_wait') and seconds(evidence['real_wait'],now) is not None and seconds(evidence['real_wait'],now)<300:
        status='WAITING FOR MARKET DATA'
    else:status='BLOCKED';blocks.append('NO_RECENT_DISCOVERY_EVIDENCE')
    # Economic progress is separate from service connectivity. No green health claim.
    stages=[{'stage':'Scan','count':funnel['scanned'],'reason':'LATEST_PASS' if funnel['scanned'] else 'NO_SCAN_EVIDENCE'},
        {'stage':'Shortlist','count':funnel['shortlisted'],'reason':'LATEST_PASS' if funnel['shortlisted'] else 'NO_RETAINED_CANDIDATES'},
        {'stage':'Astra','count':funnel['sent_to_astra'],'reason':'PROVIDER_DISPATCH_CLAIMS' if claims else (last_denial[1] if last_denial else 'NO_DISPATCH_EVIDENCE')},
        {'stage':'OPEN','count':funnel['opens'],'reason':'OPEN_DECISIONS' if funnel['opens'] else ('ASTRA_ABSTAIN: '+decisions[-1]['reason'] if decisions and decisions[-1]['action']=='ABSTAIN' else 'NO_OPEN_DECISION_RECORDED')},
        {'stage':'Broker','count':funnel['submitted'],'reason':'BROKER_ACCEPTANCE_RECORDED' if funnel['submitted'] else (drops[-1]['reason'] if drops else 'NO_BROKER_ACCEPTANCE_RECORDED')},
        {'stage':'Fill','count':funnel['filled'],'reason':'BUY_EXECUTION_RECORDED' if funnel['filled'] else ('; '.join(o['symbol']+': '+o['status'] for o in today_orders if o['side']=='BUY') or 'NO_BUY_EXECUTION_RECORDED')},
        {'stage':'Manage','count':funnel['managed'],'reason':'DURABLE_POSITION_METRICS' if managed else 'NO_POSITION_MANAGEMENT_EVIDENCE_TODAY'},
        {'stage':'Exit','count':funnel['exited'],'reason':'REALIZED_LEDGER_CLOSES' if closures else 'NO_REALIZED_CLOSE_TODAY'},
        {'stage':'Learning','count':funnel['memory_recorded'],'reason':'MEMORY_UPDATE_NOT_RECORDED' if any(x['status']!='RECORDED' for x in memory_updates) else ('CLOSED_OUTCOMES_RECORDED' if closures else 'NO_CLOSED_TRADE_TO_LEARN_FROM')}]
    progress='FILLS_RECORDED' if funnel['filled'] else ('NO_FILLS: '+stages[5]['reason'])
    # Two-brain decision flow (REAL): one row per entry decision, newest first.
    dual={str(p.get('snapshot')):p for _,p in evidence.get('real_dual',())}
    sizing={p.get('decision_id'):p for p in evidence.get('real_sizing',())}
    ticker=lambda iid:(state or {}).get('instruments',{}).get(iid,{}).get('ticker') if iid else None
    flow=[]
    for eid,at,p in reversed(list(evidence.get('real_decisions',()))[-40:]):
        dd=dual.get(str(p.get('snapshot'))) or {}
        order=next((o for o in orders.values() if o['intent'].get('plan_id')==p.get('decision_id') and o['intent']['side']=='BUY'),None)
        size=sizing.get(p.get('decision_id'))
        opus=dd.get('opus') or p.get('action')
        if opus!='OPEN':astra='NOT ASKED'
        else:astra=dd.get('astra') or ('UNAVAILABLE' if dd.get('astra_error') else 'NOT ASKED')
        if order:outcome=order.get('phase') or 'SUBMITTED'
        elif p.get('action')=='OPEN':outcome='APPROVED · NOT PLACED' if size and not size.get('placed') else 'APPROVED'
        elif opus=='OPEN':outcome='VETOED BY ASTRA' if astra in ('ABSTAIN','OPEN') else 'SKIPPED'
        else:outcome='NO TRADE'
        flow.append({'at':at,'candidates':p.get('candidates'),'shortlist':[x for x in (p.get('symbols') or []) if x][:8],
            'opus':opus,'opus_symbol':ticker(dd.get('opus_instrument')) or p.get('symbol'),'opus_reason':(p.get('reason') or '')[:400],
            'astra':astra,'astra_symbol':ticker(dd.get('astra_instrument')),'astra_reason':(dd.get('astra_reason') or dd.get('astra_error') or '')[:400],
            'consensus':dd.get('consensus') or p.get('action'),'size_weight':size.get('weight') if size else None,'outcome':outcome})
    return {'status':status,'blockers':list(dict.fromkeys(blocks)),'stages':stages,'money_path':progress,'funnel':funnel,'opportunities':opportunities,
        'decision_counts':{action:sum(d['action']==action for d in decisions) for action in ('OPEN','ABSTAIN','HOLD','MODIFY','CLOSE')},
        'orders_today':sorted(today_orders,key=lambda o:stamp(o['updated_at']),reverse=True),
        'decisions':list(reversed(decisions))[:50],'flow':flow,'rejected_opens':list(reversed(drops))[:30],
        'latency':latency[:30],'economics':economics,'learning':{'latest':latest,'groups':groups,'closed_trade_updates':memory_updates},
        'last_model_denial':{'at':last_denial[0],'reason':last_denial[1]} if last_denial else None,
        'health':{'webull':system.get('webull'),'webull_at':system.get('webull_at'),'openai':system.get('openai'),
            'openai_at':system.get('openai_at'),'reconciliation_ready':rec.get('ready'),'reconciliation_valid_until':rec.get('valid_until'),
            'settlement_known':rec.get('settlement_known'),'latches':latches,'last_error':system.get('last_error'),
            'stale_candidate_quotes':sum(r['quote_age_seconds'] is not None and r['quote_age_seconds']>2 for r in opportunities)}}
