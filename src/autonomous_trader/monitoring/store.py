"""Append-only operational evidence; accounting remains solely in the M2 ledger."""
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from psycopg import sql
from psycopg.types.json import Jsonb
from ..durable.ledger import LocalDatabase
from .measured_feedback import project_measured_feedback

SCHEMA='mvp_monitor'
MODEL_EVENT_VALUE_KEYS={'ASTRA_DECISION':'ai','ASTRA_SELECTION':'selection','ALPHA_DECISION':'ai',
    'ALPHA_DEEP_PLAN':'deep','ALPHA_POSITION_REVIEW':'ai','SHADOW_AUDIT':'shadow','MODEL_CALL_FAILED':'ai','POLICY_DECISION':'ai','MODEL_WORK_RESULT':'ai'}


def costed_model_values(payloads):
    """Billable-work evidence wins; decision/failure copies never count twice."""
    legacy=[];claims={}
    for row in payloads:
        value=next((row.get(key) for key in ('ai','deep','selection','shadow') if isinstance(row.get(key),dict)),{})
        if not value:continue
        if value.get('not_dispatched'):continue
        key=value.get('model_work_id')
        if not key:legacy.append(value)
        elif key not in claims or value.get('cost_authority')=='BILLABLE_WORK':claims[key]=value
    return legacy+list(claims.values())


def monthly_model_cost(rows):
    """Count each billable claim once; old model events supply pre-claim history."""
    claims={};results={};payloads=[]
    for kind,payload in rows:
        if kind=='MODEL_WORK_CLAIM':claims[payload['id']]=payload
        elif kind=='MODEL_WORK_RESULT':results[payload['id']]=payload
        if kind in MODEL_EVENT_VALUE_KEYS:payloads.append(payload)
    known=Decimal(0);reserved=Decimal(0);unknown=0;unbounded=False
    for value in costed_model_values(payloads):
        if value.get('model_work_id') in claims:continue
        cost=value.get('estimated_cost_usd')
        if cost is None:unknown+=1;unbounded=True
        else:known+=Decimal(str(cost))
    for key,claim in claims.items():
        cost=results.get(key,{}).get('cost_usd')
        if cost is None:
            reserved+=Decimal(str(claim['reserved_usd']));unknown+=1
        else:known+=Decimal(str(cost))
    return {'known':known,'reserved':reserved,'unknown':unknown,'unbounded':unbounded,
        'claims':claims}


def _decimal(value,default='0'):
    try:return Decimal(str(value))
    except (TypeError,ValueError,ArithmeticError):return Decimal(default)


def _compact_number(value):
    value=Decimal(value)
    text=format(value.quantize(Decimal('.000001')),'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def compact_performance_memory(outcomes,api_calls=(),limit=30):
    """Compact immutable outcome evidence without turning it into a strategy."""
    if not 1<=limit<=30:raise ValueError('INVALID_OUTCOME_LIMIT')
    groups={};broker_net_total=Decimal(0);deduplicated={}
    # Startup ledger backfill may record the same closed position seconds before
    # its richer runtime outcome. Keep ledger-only FIFO slices when no complete
    # runtime outcome exists, but never train twice on a plan with one.
    runtime_plans={row.get('plan_id') for row in outcomes if row.get('evidence_quality')=='FULL_RUNTIME'}
    for row in outcomes:
        if row.get('evidence_quality')!='FULL_RUNTIME' and row.get('plan_id') in runtime_plans:
            continue
        identity=(row.get('plan_id'),row.get('closed_at'))
        prior=deduplicated.get(identity)
        if prior is None or (prior.get('evidence_quality')!='FULL_RUNTIME' and row.get('evidence_quality')=='FULL_RUNTIME'):
            deduplicated[identity]=row
    for row in deduplicated.values():
        strategy=str(row.get('strategy_label') or 'UNCLASSIFIED')[:120]
        regime=str(row.get('market_regime') or 'UNCLASSIFIED')[:120]
        key=(strategy,regime);g=groups.setdefault(key,dict(n=0,wins=0,losses=0,net=Decimal(0),broker_net=Decimal(0),
            returns=Decimal(0),holding=Decimal(0),mfe=Decimal(0),mae=Decimal(0),model=Decimal(0),capital=Decimal(0),
            capital_time=Decimal(0),positive=Decimal(0),negative=Decimal(0),latest='',thesis='',quality=set(),
            strategy_evidence=0,nonvalidating=0))
        net=_decimal(row.get('realized_net_after_all_known_costs_usd',row.get('realized_net_before_entry_fee_usd')))
        broker_net=_decimal(row.get('realized_net_after_broker_fees_usd',row.get('realized_net_before_entry_fee_usd')))
        capital=_decimal(row.get('capital_deployed_usd'),str(_decimal(row.get('entry_usd'))*_decimal(row.get('quantity'))))
        roc=_decimal(row.get('return_on_capital'),str(net/capital if capital else 0))
        quality=str(row.get('evidence_quality') or 'LEGACY_UNSPECIFIED');g['quality'].add(quality)
        exit_reason=str(row.get('exit_reason') or 'UNSPECIFIED')
        nonvalidating=(quality!='FULL_RUNTIME' or row.get('failure_origin') in ('INFRASTRUCTURE','UNKNOWN') or exit_reason in {
            'SAFETY_LATCH','OPEN_POSITION_OUTSIDE_SESSION','RECOVERY','FORCED_LIQUIDATION'})
        g['n']+=1;g['nonvalidating']+=nonvalidating;g['strategy_evidence']+=not nonvalidating
        g['latest']=max(g['latest'],str(row.get('closed_at') or ''));g['thesis']=str(row.get('thesis') or '')[:240]
        if nonvalidating:
            g['nonvalidating_net']=g.get('nonvalidating_net',Decimal(0))+net
        else:
            g['wins']+=net>0;g['losses']+=net<0;g['net']+=net;g['broker_net']+=broker_net
            g['returns']+=roc;g['holding']+=_decimal(row.get('holding_seconds'));g['mfe']+=_decimal(row.get('maximum_favorable_excursion_usd'))
            g['mae']+=_decimal(row.get('maximum_adverse_excursion_usd'));g['model']+=_decimal(row.get('model_cost_usd'))
            g['capital']+=capital;g['capital_time']+=_decimal(row.get('capital_time_usd_seconds'))
            g['positive']+=max(Decimal(0),net);g['negative']+=max(Decimal(0),-net)
        broker_net_total+=broker_net
    api_cost=Decimal(0);api_count=0
    for value in costed_model_values(api_calls):
        cost=value.get('estimated_cost_usd') if isinstance(value,dict) else None
        if cost is not None:api_cost+=_decimal(cost);api_count+=1
    header={'memory_type':'portfolio_economics','completed_trades':sum(x['n'] for x in groups.values()),
        'realized_net_after_broker_fees_usd':_compact_number(broker_net_total),
        'all_known_api_model_cost_usd':_compact_number(api_cost),
        'economic_net_after_all_known_model_cost_usd':_compact_number(broker_net_total-api_cost),
        'costed_model_calls':api_count,'objective':'maximize_long_run_realized_net_profit'}
    entries=[]
    for (strategy,regime),g in sorted(groups.items(),key=lambda item:(item[1]['latest'],item[1]['n']),reverse=True):
        n=g['n'];evidence_n=g['strategy_evidence'];confidence=('NONE' if evidence_n==0 else
            ('EXPLORATORY' if evidence_n<3 else ('LOW' if evidence_n<10 else ('MODERATE' if evidence_n<30 else 'HIGH'))))
        entries.append({'strategy':strategy,'regime':regime,'samples':n,'sample_confidence':confidence,
            'strategy_evidence_samples':evidence_n,'nonvalidating_safety_recovery_or_legacy_samples':g['nonvalidating'],
            'score_scope':'VALIDATING_STRATEGY_OUTCOMES_ONLY','nonvalidating_net_usd':_compact_number(g.get('nonvalidating_net',0)),
            'wins':g['wins'],'losses':g['losses'],'net_after_all_known_costs_usd':_compact_number(g['net']),
            'mean_return_on_capital':(_compact_number(g['returns']/evidence_n) if evidence_n else None),'payoff_ratio':None if not g['negative'] else _compact_number(g['positive']/g['negative']),
            'mean_holding_seconds':(_compact_number(g['holding']/evidence_n) if evidence_n else None),'mean_mfe_usd':(_compact_number(g['mfe']/evidence_n) if evidence_n else None),
            'mean_mae_usd':(_compact_number(g['mae']/evidence_n) if evidence_n else None),'model_cost_usd':_compact_number(g['model']),
            'mean_capital_deployed_usd':(_compact_number(g['capital']/evidence_n) if evidence_n else None),'capital_time_usd_seconds':_compact_number(g['capital_time']),
            'evidence_quality':sorted(g['quality']),
            'legacy_zero_excursions_mean_unavailable':any(x!='FULL_RUNTIME' for x in g['quality']),
            'model_cost_scope':'PER_TRADE_COMPLETE' if g['quality']=={'FULL_RUNTIME'} else 'GLOBAL_PORTFOLIO_ONLY_FOR_LEGACY',
            'latest_thesis':g['thesis'],'latest_closed_at':g['latest']})
    chunks=[];current=[]
    for entry in entries:
        candidate=current+[entry]
        if current and len(json.dumps({'memory_type':'strategy_regime_evidence','groups':candidate},separators=(',',':'))) > 1900:
            chunks.append(json.dumps({'memory_type':'strategy_regime_evidence','groups':current},sort_keys=True,separators=(',',':')));current=[entry]
        else:current=candidate
    if current:chunks.append(json.dumps({'memory_type':'strategy_regime_evidence','groups':current},sort_keys=True,separators=(',',':')))
    recent=[]
    for row in sorted((r for r in deduplicated.values() if r.get('evidence_quality')=='FULL_RUNTIME'),
            key=lambda r:str(r.get('closed_at') or ''),reverse=True)[:6]:
        recent.append({key:row.get(key) for key in ('symbol','strategy_label','market_regime','opened_at','closed_at',
            'entry_usd','exit_usd','holding_seconds','exit_reason','realized_net_after_all_known_costs_usd',
            'broker_fees_usd','model_cost_usd','maximum_favorable_excursion_usd','maximum_adverse_excursion_usd')}
            |{'thesis':str(row.get('thesis') or '')[:100]})
    if recent and limit>=3:
        value=json.dumps({'memory_type':'recent_completed_trade_evidence','trades':recent},
            sort_keys=True,separators=(',',':'))
        while len(value)>1900 and len(recent)>1:
            recent.pop();value=json.dumps({'memory_type':'recent_completed_trade_evidence','trades':recent},
                sort_keys=True,separators=(',',':'))
        if len(value)<=1900:chunks=chunks[:limit-2]+[value]
    return tuple([json.dumps(header,sort_keys=True,separators=(',',':'))]+chunks[:limit-1])


def compact_shadow_memory(rows,limit=12):
    """Retain bounded missed-opportunity evidence without turning it into an execution rule."""
    observations=[]
    for row in rows[-limit:]:
        observations.append({key:row.get(key) for key in ('audit_id','started_at','evaluated_at','shadow_action',
            'shadow_symbol','shadow_observed_return_bps','astra_action','astra_symbol','astra_observed_return_bps','winner')})
    while observations:
        value=json.dumps({'memory_type':'shadow_opportunity_evidence','sample_policy':'exploratory_until_repeated',
            'observations':observations},sort_keys=True,separators=(',',':'))
        if len(value)<=1900:return value
        observations.pop(0)
    return None


def compact_baseline_memory(rows,limit=12):
    """Conservative missed-opportunity evidence; never an execution instruction."""
    observations=[];seen=set()
    for payload in reversed(rows):
        for row in payload.get('benchmarks',()):
            value=row.get('gross_return_bps');symbol=row.get('symbol')
            identity=(payload.get('started_at'),payload.get('evaluated_at'),symbol)
            if value is None or not symbol or identity in seen:continue
            seen.add(identity);observations.append({'started_at':payload.get('started_at'),
                'evaluated_at':payload.get('evaluated_at'),'symbol':symbol,
                'entry_usd':row.get('entry_usd'),'entry_reference':row.get('entry_reference','LEGACY_UNKNOWN'),
                'later_bid_usd':row.get('exit_usd'),
                'observed_executable_return_bps':value})
            if len(observations)>=limit:break
        if len(observations)>=limit:break
    while observations:
        value=json.dumps({'memory_type':'missed_opportunity_counterfactuals',
            'sample_policy':'exploratory_until_repeated; ask-to-later-bid; excludes slippage, fees and fill probability',
            'observations':list(reversed(observations))},sort_keys=True,separators=(',',':'))
        if len(value)<=1900:return value
        observations.pop()
    return None


def corrected_outcomes(originals,corrections):
    """Project append-only monitoring corrections without altering source events."""
    effective={event_id:payload for event_id,payload in originals}
    for correction in corrections:
        original=effective.get(correction.get('original_event_id'))
        replacement=correction.get('replacement')
        if (original is None or not isinstance(replacement,dict)
                or (original.get('plan_id'),original.get('closed_at')) !=
                   (replacement.get('plan_id'),replacement.get('closed_at'))):
            raise ValueError('INVALID_ALPHA_OUTCOME_CORRECTION')
        effective[correction['original_event_id']]=replacement
    return tuple(effective.values())

def provision(ledger_schema):
    admin=LocalDatabase('postgres','m2_admin',schema=SCHEMA)
    with admin.connect() as c:
        c.execute('CREATE SCHEMA IF NOT EXISTS mvp_monitor')
        c.execute('CREATE TABLE IF NOT EXISTS events (id text PRIMARY KEY, at timestamptz NOT NULL, kind text NOT NULL, mode text NOT NULL, payload jsonb NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS events_kind_at ON events (kind, at DESC)')  # hot-path lookups were full scans
        c.execute('CREATE TABLE IF NOT EXISTS runtime (id integer PRIMARY KEY CHECK(id=1), heartbeat timestamptz NOT NULL, payload jsonb NOT NULL)')
        for role in ('mvp_observer','mvp_reader'):
            if not c.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
                c.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(role)))
            c.execute(sql.SQL('GRANT USAGE ON SCHEMA mvp_monitor, {} TO {}').format(sql.Identifier(ledger_schema),sql.Identifier(role)))
        c.execute('GRANT SELECT,INSERT ON events TO mvp_observer')
        c.execute('GRANT SELECT,INSERT,UPDATE ON runtime TO mvp_observer')
        c.execute('GRANT SELECT ON events,runtime TO mvp_reader')
        c.execute(sql.SQL('GRANT SELECT ON {}.accounts,{}.journal TO mvp_reader').format(sql.Identifier(ledger_schema),sql.Identifier(ledger_schema)))
    return Store()

class Store:
    def __init__(self,check=lambda text:None):
        self.db=LocalDatabase('postgres','mvp_observer',schema=SCHEMA);self.check=check
    def emit(self,kind,payload,*,mode='SANDBOX',at=None,event_id=None):
        self.check(json.dumps(payload))
        with self.db.connect() as c:
            c.execute('INSERT INTO events VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (event_id or uuid4().hex,at or datetime.now(timezone.utc),kind,mode,Jsonb(payload)))
    def emit_idempotent(self,kind,payload,*,mode='SANDBOX',at=None,event_id):
        """Append once; a reused identity may never conceal different evidence."""
        self.check(json.dumps(payload))
        when=at or datetime.now(timezone.utc)
        with self.db.connect() as c:
            inserted=c.execute('INSERT INTO events VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id',
                (event_id,when,kind,mode,Jsonb(payload))).fetchone()
            if inserted:return
            prior=c.execute('SELECT kind,mode,payload FROM events WHERE id=%s',(event_id,)).fetchone()
            if prior is None or prior[0]!=kind or prior[1]!=mode or prior[2]!=payload:
                raise ValueError('MONITOR_EVENT_IDEMPOTENCY_CONFLICT')
    def heartbeat(self,payload):
        self.check(json.dumps(payload))
        with self.db.connect() as c:
            c.execute('INSERT INTO runtime VALUES(1,clock_timestamp(),%s) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat,payload=excluded.payload',(Jsonb(payload),))
    def runtime(self):
        with self.db.connect() as c:
            r=c.execute('SELECT payload FROM runtime WHERE id=1').fetchone()
            return r[0] if r else {}
    @staticmethod
    def _monthly_budget_rows(c,start,end,modes=('SANDBOX','DRY_RUN','REAL')):
        rows=c.execute("SELECT kind,payload FROM events WHERE mode=ANY(%s) AND at>=%s AND at<%s AND (kind=ANY(%s) OR kind='MODEL_WORK_CLAIM') ORDER BY at,id",
            (list(modes),start,end,list(set(MODEL_EVENT_VALUE_KEYS)-{'MODEL_WORK_RESULT'}))).fetchall()
        ids=[p['id'] for k,p in rows if k=='MODEL_WORK_CLAIM']
        # A result arriving after midnight belongs to the month of its claim.
        if ids:
            rows.extend(c.execute("SELECT kind,payload FROM events WHERE mode=ANY(%s) AND kind='MODEL_WORK_RESULT' AND payload->>'id'=ANY(%s)",(list(modes),ids)).fetchall())
        orphans=c.execute("SELECT kind,payload FROM events WHERE mode=ANY(%s) AND kind='MODEL_WORK_RESULT' AND at>=%s AND at<%s AND NOT (payload->>'id'=ANY(%s))",
            (list(modes),start,end,ids)).fetchall()
        if orphans:
            orphan_ids=[p['id'] for _,p in orphans]
            backed={r[0] for r in c.execute("SELECT payload->>'id' FROM events WHERE kind='MODEL_WORK_CLAIM' AND payload->>'id'=ANY(%s)",(orphan_ids,)).fetchall()}
            rows.extend((k,p) for k,p in orphans if p['id'] not in backed)
        # Do not charge next month's copy of an earlier month's claimed call.
        current=set(ids)
        copied={(p.get(MODEL_EVENT_VALUE_KEYS[k]) or {}).get('model_work_id') for k,p in rows if k in MODEL_EVENT_VALUE_KEYS}
        copied.discard(None);copied-=current
        if copied:
            prior={r[0] for r in c.execute("SELECT payload->>'id' FROM events WHERE kind='MODEL_WORK_CLAIM' AND payload->>'id'=ANY(%s)",(list(copied),)).fetchall()}
            rows=[(k,p) for k,p in rows if k not in MODEL_EVENT_VALUE_KEYS or (p.get(MODEL_EVENT_VALUE_KEYS[k]) or {}).get('model_work_id') not in prior]
        return rows
    def model_budget_evidence(self,start,end,*,modes=('SANDBOX','DRY_RUN','REAL')):
        with self.db.connect() as c:return self._monthly_budget_rows(c,start,end,modes)
    def model_budget_recent_claims(self,now,*,modes=('SANDBOX','DRY_RUN','REAL')):
        with self.db.connect() as c:
            return [r[0] for r in c.execute("SELECT payload FROM events WHERE mode=ANY(%s) AND kind='MODEL_WORK_CLAIM' AND at>%s AND at<=%s ORDER BY at,id",
                (list(modes),now-timedelta(seconds=300),now)).fetchall()]
    def reserve_model_claim(self,start,end,claim,allowance,*,recent_since,total_limit,
            non_position_limit,position_call_type,budget_modes=('SANDBOX','DRY_RUN','REAL'),portfolio_call_type=None,
            portfolio_session=None,portfolio_week=None,durable_position_call_type=None,
            position_session=None,position_week=None,all_calls_session=None,all_calls_week=None,
            daily_spend=None):
        """Atomically serialize cost and rolling-call admission across all workers."""
        self.check(json.dumps(claim))
        with self.db.connect() as c:
            c.execute('SELECT pg_advisory_xact_lock(481516,100)')
            recent=[r[0] for r in c.execute("SELECT payload FROM events WHERE mode=ANY(%s) AND kind='MODEL_WORK_CLAIM' AND at>%s AND at<=%s ORDER BY at,id",
                (list(budget_modes),recent_since,datetime.fromisoformat(claim['at']))).fetchall()]
            if (len(recent)>=total_limit or (claim['call_type']!=position_call_type
                    and sum(row.get('call_type')!=position_call_type for row in recent)>=non_position_limit)):
                raise ValueError('MODEL_CALL_BUDGET_WAIT')
            if daily_spend is not None:
                day_start,day_end,entry_limit,position_limit=daily_spend
                day=monthly_model_cost(self._monthly_budget_rows(c,day_start,day_end,budget_modes))
                limit=position_limit if claim['call_type']==position_call_type else entry_limit
                if day['known']+day['reserved']+Decimal(claim['reserved_usd'])>limit:
                    raise ValueError('MODEL_DAILY_SPEND_LIMIT')
            if claim['call_type']==portfolio_call_type:
                for bounds,reason in ((portfolio_session,'PORTFOLIO_SESSION_CALL_LIMIT'),
                                      (portfolio_week,'PORTFOLIO_WEEK_CALL_LIMIT')):
                    if bounds is None:raise ValueError('PORTFOLIO_CALL_HISTORY_UNAVAILABLE')
                    lower,upper,limit=bounds
                    count=c.execute("SELECT count(*) FROM events WHERE mode=ANY(%s) AND kind='MODEL_WORK_CLAIM' AND payload->>'call_type'=%s AND at>=%s AND at<%s",
                        (list(budget_modes),portfolio_call_type,lower,upper)).fetchone()[0]
                    if count>=limit:raise ValueError(reason)
            if durable_position_call_type is not None and claim['call_type']==durable_position_call_type:
                for bounds,reason in ((position_session,'POSITION_SESSION_CALL_LIMIT'),
                                      (position_week,'POSITION_WEEK_CALL_LIMIT')):
                    if bounds is None:raise ValueError('POSITION_CALL_HISTORY_UNAVAILABLE')
                    lower,upper,limit=bounds
                    count=c.execute("SELECT count(*) FROM events WHERE mode=ANY(%s) AND kind='MODEL_WORK_CLAIM' AND payload->>'call_type'=%s AND at>=%s AND at<%s",
                        (list(budget_modes),durable_position_call_type,lower,upper)).fetchone()[0]
                    if count>=limit:raise ValueError(reason)
            for bounds,reason in ((all_calls_session,'REAL_SESSION_CALL_LIMIT'),
                                  (all_calls_week,'REAL_WEEK_CALL_LIMIT')):
                if bounds is not None:
                    lower,upper,limit=bounds
                    count=c.execute("SELECT count(*) FROM events WHERE mode=ANY(%s) AND kind='MODEL_WORK_CLAIM' AND at>=%s AND at<%s",
                        (list(budget_modes),lower,upper)).fetchone()[0]
                    if count>=limit:raise ValueError(reason)
            rows=self._monthly_budget_rows(c,start,end,budget_modes)
            state=monthly_model_cost(rows)
            suspended=allowance is None
            if suspended and (claim.get('mode')!='REAL'
                    or claim.get('monthly_cost_control')!='OWNER_TEMPORARY_CAP_SUSPENSION_V1'
                    or claim.get('monthly_cost_suspension_id')!='owner-real-monthly-cap-suspension-2026-09-30'):
                raise ValueError('MODEL_MONTHLY_BUDGET_AUTHORITY_REQUIRED')
            if not suspended and (state['unbounded']
                    or state['known']+state['reserved']+Decimal(claim['reserved_usd'])>allowance):
                raise ValueError('MODEL_MONTHLY_BUDGET_EXHAUSTED')
            c.execute('INSERT INTO events VALUES(%s,%s,%s,%s,%s)',
                (claim['id'],datetime.fromisoformat(claim['at']),'MODEL_WORK_CLAIM',claim['mode'],Jsonb(claim)))
    def pipeline_state(self,mode):
        with self.db.connect() as c:
            row=c.execute("SELECT payload FROM events WHERE kind='OPPORTUNITY_QUEUE' AND mode=%s ORDER BY COALESCE((payload->>'revision')::bigint,0) DESC,at DESC LIMIT 1",(mode,)).fetchone()
            return row[0] if row else None
    def plan(self,decision_id):
        with self.db.connect() as c:
            row=c.execute("SELECT payload->'ai'->'decision' FROM events WHERE kind='ASTRA_DECISION' AND payload->'ai'->>'schema_valid'='true' AND payload->'ai'->>'semantics_valid'='true' AND payload->'ai'->'decision'->>'decision_id'=%s ORDER BY at DESC LIMIT 1",(decision_id,)).fetchone()
            return row[0] if row else None
    def amended_plan(self,plan_id):
        with self.db.connect() as c:
            row=c.execute("SELECT payload FROM events WHERE kind='POSITION_PLAN_AMENDED' AND payload->>'plan_id'=%s ORDER BY at DESC LIMIT 1",(plan_id,)).fetchone()
            return row[0] if row else None
    def alpha_plan(self,plan_id):
        with self.db.connect() as c:
            row=c.execute("SELECT payload->'plan' FROM events WHERE kind IN ('ALPHA_PLAN_CREATED','ALPHA_PLAN_MODIFIED') AND payload->>'plan_id'=%s ORDER BY at DESC LIMIT 1",(plan_id,)).fetchone()
            return row[0] if row else None
    def original_alpha_plan(self,plan_id):
        with self.db.connect() as c:
            row=c.execute("SELECT payload->'plan' FROM events WHERE kind='ALPHA_PLAN_CREATED' AND payload->>'plan_id'=%s ORDER BY at,id LIMIT 1",(plan_id,)).fetchone()
            return row[0] if row else None
    def alpha_metrics(self,plan_id,instrument_id):
        with self.db.connect() as c:
            row=c.execute("SELECT payload FROM events WHERE kind='ALPHA_POSITION_METRICS' AND payload->>'plan_id'=%s AND payload->>'instrument_id'=%s ORDER BY at DESC LIMIT 1",(plan_id,instrument_id)).fetchone()
            return row[0] if row else None
    def shadow_audit(self,audit_id):
        with self.db.connect() as c:
            row=c.execute("SELECT payload->'shadow'->'audit' FROM events WHERE kind='SHADOW_AUDIT' AND payload->>'audit_id'=%s ORDER BY at DESC LIMIT 1",(audit_id,)).fetchone()
            return row[0] if row else None
    def recent_outcomes(self,limit=30):
        with self.db.connect() as c:
            originals=c.execute("SELECT id,payload FROM events WHERE kind='ALPHA_OUTCOME' AND mode='SANDBOX' ORDER BY at,id",()).fetchall()
            corrections=[row[0] for row in c.execute("SELECT payload FROM events WHERE kind='ALPHA_OUTCOME_CORRECTION' AND mode='SANDBOX' ORDER BY at,id",()).fetchall()]
            outcomes=corrected_outcomes(originals,corrections)
            calls=[row[0] for row in c.execute("SELECT payload FROM events WHERE kind = ANY(%s) AND mode='SANDBOX' ORDER BY at",(list(MODEL_EVENT_VALUE_KEYS),)).fetchall()]
            shadows=[row[0] for row in c.execute("SELECT payload FROM events WHERE kind='SHADOW_RESULT' AND mode='SANDBOX' ORDER BY at",()).fetchall()]
            baselines=[row[0] for row in c.execute("SELECT payload FROM events WHERE kind='BASELINE_RESULT' AND mode='SANDBOX' ORDER BY at",()).fetchall()]
            extras=[]
            shadow=compact_shadow_memory(shadows)
            baseline=compact_baseline_memory(baselines)
            if shadow:extras.append(shadow)
            if baseline:extras.append(baseline)
            missed=[r[0] for r in c.execute("SELECT DISTINCT ON (payload->>'decision_id') payload,at FROM events WHERE kind='OPPORTUNITY_OUTCOME' AND mode='SANDBOX' ORDER BY payload->>'decision_id',at DESC").fetchall()]
            missed=sorted(missed,key=lambda r:r.get('evaluated_at',''),reverse=True)[:6]
            compact=[{k:r.get(k) for k in ('decision_id','symbol','strategy','action','execution_block',
                'failure_origin','entry_reference_ask','later_bid','mfe_bps','mae_bps','model_cost_usd')}
                |{'thesis':str(r.get('thesis') or '')[:120]} for r in missed]
            while compact:
                value=json.dumps({'memory_type':'execution_opportunity_evidence','realized_pnl':None,
                    'sample_policy':'EXPLORATORY_NO_ASSUMED_FILL_OR_PROFIT_INFRASTRUCTURE_IS_NOT_STRATEGY_FAILURE',
                    'observations':compact},separators=(',',':'))
                if len(value)<=1900:extras.append(value);break
                compact.pop()
            memory=list(compact_performance_memory(outcomes,calls,max(1,limit-len(extras))))+extras
            return tuple(memory[:limit])
    def measured_portfolio_feedback(self,as_of):
        """Project only evidence known before a later portfolio snapshot."""
        with self.db.connect() as c:
            decisions=c.execute("SELECT at,payload FROM events WHERE kind='PORTFOLIO_DECISION_EVIDENCE' AND mode='SANDBOX' AND at<%s ORDER BY at,id",(as_of,)).fetchall()
            originals=c.execute("SELECT id,payload FROM events WHERE kind='ALPHA_OUTCOME' AND mode='SANDBOX' AND at<%s ORDER BY at,id",(as_of,)).fetchall()
            corrections=[row[0] for row in c.execute("SELECT payload FROM events WHERE kind='ALPHA_OUTCOME_CORRECTION' AND mode='SANDBOX' AND at<%s ORDER BY at,id",(as_of,)).fetchall()]
            outcomes=corrected_outcomes(originals,corrections)
            results=c.execute("SELECT at,payload FROM events WHERE kind='MODEL_WORK_RESULT' AND mode='SANDBOX' AND at<%s ORDER BY at,id",(as_of,)).fetchall()
        return project_measured_feedback(decisions,outcomes,results,as_of=as_of)
    def import_existing(self,root):
        for name in ('astra-decision-result.json','mvp-real-cycle.json'):
            p=Path(root)/'docs'/name
            if not p.exists():continue
            report=json.loads(p.read_text(encoding='utf-8'));ai=report.get('ai',report)
            decision=ai.get('decision')
            if decision:
                self.emit('ASTRA_DECISION',{'ai':ai,'origin':'historical-real-api'},at=decision['created_at'],event_id='decision-'+decision['decision_id'])
