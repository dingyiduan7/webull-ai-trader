"""Dormant daily REAL preparation/launcher. Installation is a separate owner step."""
from datetime import datetime,timezone
from pathlib import Path
import json
import os
import subprocess
import sys
from time import sleep as readiness_sleep
from math import isfinite
from contextlib import contextmanager
from uuid import uuid4
from threading import Event

_SERVICE_STOP = None


class ServiceStopping(BaseException):
    pass


def check_service_stop():
    if _SERVICE_STOP is not None and _SERVICE_STOP.is_set():
        raise ServiceStopping()


def service_sleep(seconds):
    if _SERVICE_STOP is None:
        readiness_sleep(seconds)
    elif _SERVICE_STOP.wait(seconds):
        raise ServiceStopping()

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
from autonomous_trader.runtime_storage import data_root, postgres_bin
from autonomous_trader.real_daily import (load_authority,session_entitlement,folder,certificate,
    persist_certificate,current_certificate,verify_flat_account,eligible_incidents,AUTHORITY_NAME)
from autonomous_trader.mvp.config import reviewed_calendar
from autonomous_trader.reference import NEW_YORK
from autonomous_trader.real_state import digest


@contextmanager
def launcher_lock(root):
    import msvcrt
    directory=data_root(root)/'.local/real-supervisor';directory.mkdir(parents=True,exist_ok=True)
    with (directory/'daily.lock').open('a+b') as handle:
        if handle.seek(0,2)==0:handle.write(b'0');handle.flush()
        handle.seek(0)
        try:msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise ValueError('REAL_DAILY_ALREADY_ACTIVE') from None
        try:yield
        finally:handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)


def ensure_database(root):
    hidden={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    result=subprocess.run([str(postgres_bin(root)/'pg_ctl.exe'),
        '-D',str(data_root(root)/'.local/pgdata-m2'),'status'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**hidden)
    if result.returncode:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-File',
            str(root/'scripts/local_postgres.ps1'),'start','-DataRoot',str(data_root(root))],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**hidden)
        if result.returncode:raise ValueError('REAL_DAILY_DATABASE_UNAVAILABLE')


def aligned_preparation_packet(packet,quotes,received,now):
    """One final snapshot binds portfolio and coverage; account observations remain unchanged."""
    from dataclasses import replace
    from decimal import Decimal
    from scripts.webull_real_readiness_packet import _time
    from autonomous_trader.real_market import _spread_bps
    expected={q.symbol:q for q in packet.context.instruments}
    required=set(expected)|{'AAPL','QQQ'}
    if not isinstance(quotes,list) or len(quotes)!=len(required):raise ValueError('REAL_COVERAGE_SNAPSHOT')
    by_symbol={q.get('symbol'):q for q in quotes}
    if set(by_symbol)!=required:raise ValueError('REAL_COVERAGE_SNAPSHOT')
    updated=[];receipts={}
    session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
    for symbol,item in expected.items():
        q=by_symbol[symbol]
        if str(q.get('instrument_id'))!=item.instrument_id:raise ValueError('REAL_PROFILE_QUOTE_BINDING')
        source_at=_time(q['quote_time'])
        bid=Decimal(str(q['bid']));ask=Decimal(str(q['ask']))
        receipt=received[symbol] if isinstance(received,dict) else received
        # A candidate whose quote or its receipt would fail the packet's 10-second checks is left out here, the
        # same as a stale quote (2026-10-30: one such candidate blocked the whole daily start). Held instruments
        # are never left out: they still fail loudly in validate().
        unusable=(not session.opens_at<=source_at<=now or (now-source_at).total_seconds()>10 or not 0<bid<=ask
            or not source_at<=receipt<=now or (now-receipt).total_seconds()>10)
        if unusable and item.instrument_id not in packet.positions:
            continue
        if not 0<bid<=ask:raise ValueError('INVALID_REAL_QUOTE')
        updated.append(item.model_copy(update=dict(quote_at=_time(q['quote_time']),bid_usd=bid,ask_usd=ask,
            reference_price_usd=(bid+ask)/2,spread_bps=_spread_bps(bid,ask),
            bid_size=q.get('bid_size'),ask_size=q.get('ask_size'),volume=q.get('volume'))))
        receipts[item.instrument_id]=receipt
    if not updated:raise ValueError('NO_FRESH_REAL_PORTFOLIO_CONTEXT')
    ids={q.instrument_id for q in updated}
    refs=tuple(i for i in packet.instruments if i.instrument_id in ids)
    elapsed=(now-packet.context.as_of).total_seconds()
    if elapsed<0:raise ValueError('STALE_REAL_CONTEXT')
    proof={**packet.market_data_evidence,
        'nasdaq_symbols':[q.symbol for q in updated],
        'quote_max_age_ms':max((now-q.quote_at).total_seconds()*1000 for q in updated),
        'receipt_max_age_ms':max((now-at).total_seconds()*1000 for at in receipts.values()),
        'bar_max_age_seconds':packet.market_data_evidence['bar_max_age_seconds']+elapsed}
    context=packet.context.model_copy(update=dict(as_of=now,instruments=tuple(updated)))
    result=replace(packet,instruments=refs,context=context,quote_received_at=receipts,market_data_evidence=proof)
    # Carried positions were verified against the ledger before this point (verify_flat_account).
    result.validate(now,flat=not packet.positions,entry=True);result.validate_market_data_authority(now)
    return result


def coverage_snapshots(http,symbols):
    """Respect broker batch size without imposing a portfolio candidate ceiling."""
    from autonomous_trader.sandbox.http import Operation,build_request
    quotes=[];receipts={}
    from autonomous_trader.real_market import snapshot_request
    for start in range(0,len(symbols),20):
        group=symbols[start:start+20]
        rows=http.read(snapshot_request(group))
        if not isinstance(rows,list) or len(rows)!=len(group) or {row.get('symbol') for row in rows}!=set(group):
            raise ValueError('REAL_COVERAGE_SNAPSHOT')
        quotes.extend(rows)
        receipts.update({symbol:http.last_received_at for symbol in group})
    return quotes,receipts


def current_preparation_account(packet,account,state,authority,now):
    """Fresh independent cash/ownership observation; never re-date quote sources."""
    from dataclasses import replace
    from decimal import Decimal
    from autonomous_trader.real_trading import flat_recovery_values
    from autonomous_trader.real_daily import carried_positions_allowed
    try:
        if carried_positions_allowed(state,authority):
            from autonomous_trader.carry import carry_account_values
            settled,unsettled,_=carry_account_values(account,state,authority['account_binding'],now)
            equity=Decimal(account['cash']['net_liquidation_value'])
        else:
            settled,unsettled,equity=flat_recovery_values(account,state,authority['account_binding'],now)
    except ValueError as exc:
        # Broker-truth (owner 2026-10-18): preparation is read-only on the ledger, so a cash difference here is
        # expected; it is accepted only when the trader will adopt it (valid, quiet, positions identical).
        if str(exc) not in ('REAL_RECONCILIATION_RECOVERY_CASH_MISMATCH','REAL_CARRY_CASH_MISMATCH'):raise
        from autonomous_trader.broker_truth import plan,valid_observation
        from autonomous_trader.carry import broker_positions_by_instrument
        cash=account['cash']
        settled,unsettled=Decimal(cash['settled_cash']),Decimal(cash['unsettled_cash'])
        equity=Decimal(cash['net_liquidation_value'])
        positions=broker_positions_by_instrument(account,state)
        checks=dict(valid=valid_observation(account,authority['account_binding'],now),
            eligible=plan(state,settled,unsettled,now,broker_positions=positions,
                broker_open_ids=tuple(account.get('open_client_ids') or ())) is not None,
            equity_consistent=bool(positions) or equity==settled+unsettled)
        try:
            # Cash figures and booleans only (no identifiers or secrets): makes any refusal diagnosable.
            from autonomous_trader.broker_truth import quiet
            (data_root(ROOT)/'.local/real-readiness/prep-cash-diagnostic.json').write_text(json.dumps(dict(
                at=now.isoformat(),checks=checks,ledger_quiet=quiet(state),
                broker=dict(settled=str(settled),unsettled=str(unsettled),total=str(cash.get('cash_balance')),equity=str(equity),
                    positions=len(positions),open_orders=len(account.get('open_client_ids') or ())),
                ledger=dict(settled=state['cash']['settled_usd'],unsettled=state['cash']['unsettled_usd'],
                    equity=state['cash']['liquidation_equity_usd'],positions=len(state['positions']))),indent=1),encoding='utf-8')
        except Exception:pass
        if not all(checks.values()):raise
    # Account and market observations are independently timed. The caller aligns
    # the newly collected quotes and validates the whole packet immediately next.
    context=packet.context.model_copy(update={'settled_cash_usd':min(Decimal(500),settled)})
    return replace(packet,observed_at=account['observed_at'],settled=settled,
        unsettled=unsettled,equity=equity,context=context)



def prepare_market(root,authority,clock,source,state,http=None):
    """Full ordinary entry certificate; caller already owns the trading writer."""
    from autonomous_trader.sandbox.http import Operation,build_request
    from scripts.webull_real_readiness_packet import coverage_sample,coverage_batch_bars,coverage_delay_evidence,_time
    http=http if http is not None else source.http
    session=reviewed_calendar().session(clock().astimezone(NEW_YORK).date())
    samples=[coverage_sample(http,'AAPL','STOCK',session,clock()),coverage_sample(http,'QQQ','ETF',session,clock())]
    bars_request=build_request(Operation.HISTORY_BATCH,symbols=('AAPL','QQQ'));bars_request.set_real_time_required(True)
    body=http.read(bars_request);bars=coverage_batch_bars(body,samples,http.last_received_at,clock())
    samples=[{**row,**bars[row['symbol']],'bars_real_time_required':True} for row in samples]
    source.discover();packet=source.refresh(state)
    verify_flat_account(state,packet,authority,clock())
    # Re-read coverage snapshots after slow discovery/history; keep source timestamps.
    symbols=tuple(dict.fromkeys(('AAPL','QQQ')+tuple(q.symbol for q in packet.context.instruments)))
    quotes,received=coverage_snapshots(http,symbols)
    packet=current_preparation_account(packet,source.account_observation(),state,authority,clock())
    packet=aligned_preparation_packet(packet,quotes,received,clock())
    if not isinstance(quotes,list) or len(quotes)!=len(symbols):raise ValueError('REAL_COVERAGE_SNAPSHOT')
    by_symbol={q['symbol']:q for q in quotes}
    if set(by_symbol)!=set(symbols):raise ValueError('REAL_COVERAGE_SNAPSHOT')
    for row in samples:
        quote=by_symbol[row['symbol']]
        if str(quote.get('instrument_id'))!=row['instrument_id']:raise ValueError('REAL_COVERAGE_ASSET_IDENTITY')
        row.update(quote_at=_time(quote['quote_time']).isoformat(),quote_received_at=received[row['symbol']].isoformat(),
            delay_evidence=coverage_delay_evidence(quote))
        row.pop('instrument_id')
    verify_flat_account(state,packet,authority,clock())
    value=certificate(authority,packet,state,samples,clock(),root)
    return value


def open_first_ledger(root,authority,db,ledger,clock):
    """This copy has no REAL ledger yet: open it from the verified V3 bootstrap checkpoint."""
    from autonomous_trader.durable.ledger import migrate,grant_writer
    from autonomous_trader.real_market import RealMarketSource
    from autonomous_trader.real_secrets import load_real_credentials
    from autonomous_trader.real_trading import RealGate,open_ledger_from_checkpoint
    checkpoint,_=RealGate(root,authority['account_binding']).checkpoint(authority['flat_checkpoint_sha256'])
    entitlement=session_entitlement(authority,clock(),root)
    market={'session_label':clock().astimezone(NEW_YORK).date().isoformat(),'entitlement_evidence_sha256':digest(entitlement)}
    source=RealMarketSource(load_real_credentials(root),authority['account_binding'],clock,market_authority=market)
    source.discover();packet=source.refresh()
    migrate(db);grant_writer(db,'m2_writer')
    open_ledger_from_checkpoint(ledger,checkpoint,packet,clock)
    # A newly created ledger starts unreconciled with settlement unknown. The packet was just
    # proven flat and cash-identical to the checkpoint, so record that as the first reconciliation.
    from autonomous_trader.durable.models import Reconcile
    from uuid import uuid4
    holder='real-first-open';account=authority['account_binding']
    leader=ledger.acquire_leader(account,holder,event_id=uuid4().hex,at=clock(),ttl_seconds=1)  # short: never blocks the runner
    ledger.execute(account,Reconcile(settled_usd=packet.settled,unsettled_usd=packet.unsettled,positions=(),
        outstanding_intent_ids=(),evidence_id=packet.evidence_id,settlement_known=True),event_id=uuid4().hex,
        expected_version=leader['version'],holder=holder,token=leader['leader']['token'],at=clock())
    return ledger.rebuild(authority['account_binding'])


def collect(root,authority,clock):
    """No model, token creation, order/cancel endpoint or live ledger writes."""
    from autonomous_trader.durable.ledger import LocalDatabase,Ledger
    from autonomous_trader.durable.projection import Rejected
    from autonomous_trader.real_secrets import load_real_credentials
    from autonomous_trader.real_market import RealMarketSource
    from autonomous_trader.real_strategy_broker import RealStrategyHttp
    from autonomous_trader.real_trading import RealGate
    from autonomous_trader.sandbox.http import Operation,build_request
    from scripts.webull_real_readiness_packet import (coverage_sample,coverage_batch_bars,
        coverage_delay_evidence,_time)
    db=LocalDatabase('postgres','m2_admin',schema='real_trader')
    lock=db.connect();lock.autocommit=True
    try:
        if not lock.execute('SELECT pg_try_advisory_lock(%s)',(17482026,)).fetchone()[0]:
            raise ValueError('REAL_RUNNER_ALREADY_ACTIVE')
        ledger=Ledger(db,execution_scope='REAL')
        try:state=ledger.rebuild(authority['account_binding'])
        except Exception as exc:
            # A brand-new machine has either no account row or no ledger schema at all.
            if str(exc)!='UNKNOWN_ACCOUNT' and type(exc).__name__!='UndefinedTable':raise
            state=open_first_ledger(root,authority,db,ledger,clock)
        eligible_incidents(state,clock().astimezone(NEW_YORK).date())
        from autonomous_trader.real_daily import carried_positions_allowed
        if ((state['positions'] and not carried_positions_allowed(state,authority))
                or any(not r['final'] for r in state['orders'].values())):
            raise ValueError('REAL_DAILY_UNRESOLVED_EXPOSURE_OR_ORDER')
        creds=load_real_credentials(root)
        if not creds.access_token:raise ValueError('REAL_STORED_TOKEN_REQUIRED')
        entitlement=session_entitlement(authority,clock(),root)
        market={'session_label':clock().astimezone(NEW_YORK).date().isoformat(),
            'entitlement_evidence_sha256':digest(entitlement)}
        source=RealMarketSource(creds,authority['account_binding'],clock,market_authority=market)
        # Same bounded fee-surplus explanation as the trader (cash_precision); never a tolerance.
        from types import SimpleNamespace
        from autonomous_trader.cash_precision import make_adjuster
        source.cash_adjuster=make_adjuster(SimpleNamespace(state=state),source,clock)
        http=RealStrategyHttp(creds);session=reviewed_calendar().session(clock().astimezone(NEW_YORK).date())
        value=prepare_market(root,authority,clock,source,state,http=http)
        # Confirm the journal/projection stayed at the observed version under our writer lock.
        if ledger.snapshot(authority['account_binding'])!=state:raise ValueError('REAL_DAILY_LEDGER_CHANGED')
        RealGate(root,authority['account_binding']).checkpoint(authority['flat_checkpoint_sha256'])
        text=json.dumps(value,sort_keys=True)
        if any(secret and secret in text for secret in (creds.app_key,creds.app_secret,creds.account_id,creds.access_token)):
            raise ValueError('REAL_SECRET_SANITIZATION_FAILURE')
        persist_certificate(root,value)
        return value
    finally:lock.close()


def carried_positions_present(root,authority):
    """Read-only ledger check (no writer lock needed): does the REAL ledger hold positions?"""
    try:
        from autonomous_trader.durable.ledger import LocalDatabase,Ledger
        state=Ledger(LocalDatabase('postgres','m2_admin',schema='real_trader'),execution_scope='REAL').snapshot(authority['account_binding'])
        return bool(state['positions'])
    except Exception:return False


def apply_releases(root):
    try:
        import importlib.util
        spec=importlib.util.spec_from_file_location('install_reviewed',str(Path(root)/'install_reviewed.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        results=module.apply_pending(Path(root))
    except Exception as exc:
        results=[{'status':'INSTALLER_UNAVAILABLE','reason':type(exc).__name__}]
    if results:
        directory=data_root(root)/'.local/releases';directory.mkdir(parents=True,exist_ok=True)
        (directory/'last-apply.json').write_text(json.dumps({'at':datetime.now(timezone.utc).isoformat(),
            'results':results}),encoding='utf-8')
    return results


def exception_detail(exc):
    """Class name and code locations only (file:line:function): never exception text, which may carry provider data."""
    import traceback
    try:
        frames=[Path(f.filename).name+':'+str(f.lineno)+':'+f.name for f in traceback.extract_tb(exc.__traceback__)][-6:]
        value=dict(error_class=type(exc).__name__,frames=frames)
        # The HTTP status number only (2026-10-30: a refused quote batch left no clue what Webull returned).
        import re
        match=re.fullmatch(r'REAL_HTTP_(\d{3})_.*',str(exc)) if type(exc) is ValueError else None
        if match:value['http_status']=int(match.group(1))
        elif type(exc) is ValueError and re.fullmatch(r'REAL_[A-Z0-9_]{1,60}',str(exc)):value['code']=str(exc)
        return value
    except Exception:return dict(error_class='UNKNOWN')


def blocked(root,code,now,detail=None):
    # Only fixed classes/codes leave the exception boundary. Never raw broker text.
    directory=folder(root);directory.mkdir(parents=True,exist_ok=True)
    path=directory/('daily-blocked-'+uuid4().hex+'.json')
    with path.open('x',encoding='utf-8') as out:
        value=dict(schema='REAL_DAILY_BLOCKED_V1',at=now.isoformat(),reason=code,production_enabled=False)
        if detail:value['detail']=detail
        json.dump(value,out)


def run(root=ROOT,clock=lambda:datetime.now(timezone.utc),*,prepare_only=False,retry_readiness=False,sleep=readiness_sleep):
    now=clock();authority=load_authority(root,now)  # File-only, before all runtime touches.
    check_service_stop()
    session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
    if not session or not session.permits_entry(now):return 'CLOSED'
    with launcher_lock(root):
        check_service_stop()
        ensure_database(root)
        check_service_stop()
        marker=data_root(root)/'.local/real-supervisor/launched.json'
        previous=json.loads(marker.read_text()) if marker.exists() else {}
        if previous.get('authority_session')!=session.label.isoformat():
            # First start of this trading day: the only safe point to install
            # pending releases (no trader child exists). A failed bundle is rolled back and never blocks.
            # With carried positions this is still safe: the owner authority binds the running build at load
            # time, and exits keep working through RealGate._protective_receipt until today's certificate exists.
            apply_releases(root)
        if marker.exists():
            if previous.get('authority_session')==session.label.isoformat():
                # Same-day restart never replaces the certificate or regains first-start authority.
                from autonomous_trader.real_waiting import service_receipt
                service_receipt(root,clock())
                if prepare_only:return 'ALREADY_PREPARED'
                return supervise(root)
        from autonomous_trader.real_read_budget import ReadRateLimited
        from autonomous_trader.real_waiting import market_unavailable
        delay=5
        while True:
            now=clock();authority=load_authority(root,now)
            check_service_stop()
            current=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
            if not current or current.label!=session.label or not current.permits_entry(now):return 'CLOSED'
            session_entitlement(authority,now,root)
            try:
                check_service_stop()
                prepared_digest=digest(collect(root,authority,clock))
                check_service_stop()
                break
            except Exception as exc:
                transient=(type(exc) is ReadRateLimited or
                    (type(exc) is ValueError and str(exc) in {
                        'REAL_TRANSPORT_UNCERTAIN_NO_RETRY','REAL_HTTP_503_UNAVAILABLE'}))
                if retry_readiness and not prepare_only and (transient or market_unavailable(exc)):
                    code=('REAL_READ_RATE_LIMITED' if type(exc) is ReadRateLimited else
                        'REAL_DAILY_READINESS_UNAVAILABLE')
                    blocked(root,code,clock(),dict(error_class=type(exc).__name__,route=getattr(exc,'path',None),
                        retry_after=getattr(exc,'retry_after',None)))
                    wait=delay
                    if type(exc) is ReadRateLimited:
                        retry_after=float(exc.retry_after)
                        if not isfinite(retry_after):raise ValueError('REAL_DAILY_PREPARATION_BLOCKED') from None
                        wait=max(wait,retry_after)
                    # Bounded backoff, not a total outage/retry limit. Recheck offline
                    # authority and session during long provider cooldowns as well.
                    while wait>0:
                        now=clock();latest=load_authority(root,now)
                        current=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
                        if not current or current.label!=session.label or not current.permits_entry(now):return 'CLOSED'
                        session_entitlement(latest,now,root)
                        pause=min(wait,60);sleep(pause);wait-=pause
                    delay=min(delay*2,60)
                    continue
                if not market_unavailable(exc):
                    # Carried positions must never go unwatched because today's entry preparation failed:
                    # start the observation/protection child (reduce-only) and record why entries are off.
                    if not prepare_only and carried_positions_present(root,authority):
                        blocked(root,'REAL_DAILY_PREPARATION_BLOCKED_PROTECTING_CARRIED_POSITIONS',clock(),exception_detail(exc))
                        return supervise(root)
                    raise
                if prepare_only:return 'WAITING_FOR_MARKET_DATA'
                return supervise(root)
        if prepare_only:return 'PREPARED'
        # This session certificate authorizes startup, not use of saved prices.
        # Composition independently refreshes account/market evidence. The protected
        # build audit may outlast quote freshness; retain actual certificate expiry
        # and bind the post-audit check to exactly what this collection produced.
        latest=load_authority(root,clock())
        value,_=current_certificate(root,latest,clock())
        if latest!=authority or digest(value)!=prepared_digest:
            raise ValueError('REAL_DAILY_START_EVIDENCE_EXPIRED')
        return supervise(root)


def advisory_arguments(root):
    """Explicit local integration binding only; optional advice cannot block startup."""
    from autonomous_trader.decision_api.sme_advisory import rotating_configuration,_read_json
    try:
        binding=_read_json(data_root(root)/'.local/real-readiness/sme-advisory-integration.json')
        if binding.get('schema')!='REAL_SME_ADVISORY_INTEGRATION_V1' or binding.get('enabled') is not True:return []
        path=Path(binding['provider_config']);pinned=binding['provider_config_sha256']
        if not path.is_absolute() or rotating_configuration(path,pinned) is None:return []
        return ['--sme-advisory-provider',str(path),'--sme-advisory-provider-sha256',pinned]
    except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):return []


def supervise(root):
    check_service_stop()
    from scripts.owned_process import spawn, confirm_stop
    hidden={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    env=dict(os.environ)
    # Pass the owner-selected Claude key LOCATION to the child; never read the key here.
    config=data_root(root)/'.local/mvp/owner-config.json'
    if not env.get('ANTHROPIC_API_KEY') and not env.get('ANTHROPIC_API_KEY_FILE') and config.is_file():
        value=json.loads(config.read_text(encoding='utf-8-sig')).get('anthropic_key_file')
        # Absent: the child uses the DPAPI-encrypted .secrets/anthropic.clixml.
        if value is not None and (not isinstance(value,str) or not value):raise ValueError('REAL_MODEL_KEY_LOCATION_REQUIRED')
        if value:env['ANTHROPIC_API_KEY_FILE']=value
    # Second brain (Astra, dual-brain consensus): pass the owner-selected OpenAI key LOCATION, never the key.
    if not env.get('OPENAI_API_KEY') and not env.get('OPENAI_API_KEY_FILE') and config.is_file():
        value=json.loads(config.read_text(encoding='utf-8-sig')).get('openai_key_file')
        if isinstance(value,str) and value:env['OPENAI_API_KEY_FILE']=value
    check_service_stop()
    child=spawn([sys.executable,'-I','-B',str(root/'scripts/run_real_supervisor.py'),*advisory_arguments(root)],cwd=root,
        env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stop=_SERVICE_STOP,**hidden)
    try:
        while child.poll() is None:
            if _SERVICE_STOP is not None and _SERVICE_STOP.wait(2):
                raise ServiceStopping()
            if _SERVICE_STOP is None:readiness_sleep(2)
    finally:
        confirm_stop(child)
    if child.wait():raise ValueError('REAL_DAILY_SUPERVISOR_BLOCKED')
    return 'CLOSED_RECONCILED'



def resident(stop, root=ROOT, clock=lambda:datetime.now(timezone.utc), report=None):
    """Keep availability alive; every work attempt uses the existing full gates.

    Closed-session run() is file-only. Failed account/authority/order checks
    remain failures; waiting does not clear them or grant execution authority.
    Availability reports contain no financial data and grant no authority.
    """
    global _SERVICE_STOP
    previous_stop=_SERVICE_STOP
    _SERVICE_STOP=stop
    delay=5
    try:
        while not stop.is_set():
            try:
                result=run(root,clock,retry_readiness=True,sleep=service_sleep)
                status='STANDBY' if result in ('CLOSED','CLOSED_RECONCILED') else 'BLOCKED'
                reason=result if status=='STANDBY' else 'UNEXPECTED_LIFECYCLE_RETURN'
                delay=5
            except ServiceStopping:
                break
            except Exception:
                # Do not treat failure as closure, clear latches, re-date
                # observations, or expose raw provider/configuration errors.
                status='BLOCKED';reason='GUARDED_STARTUP_OR_RECOVERY_UNAVAILABLE'
                delay=min(delay*2,60)
            if report is not None:
                report(dict(status=status,reason=reason,at=clock().isoformat(),
                            trading_readiness_verified=False))
            # Reuse the readiness loop's existing maximum poll/backoff cadence.
            # SCM supervises this process; it is not another trader watchdog.
            if stop.wait(60 if status=='STANDBY' else delay):break
    finally:
        _SERVICE_STOP=previous_stop


STALE_EVIDENCE_REASONS=frozenset(('REAL_PERSISTENT_AUTHORITY_INVALID','REAL_FLAT_CHECKPOINT_REQUIRED','REAL_ENTITLEMENT_UNVERIFIED',
    'REAL_FLAT_CHECKPOINT_BINDING'))


def checkpoint_binding_check(root,authority):
    """The evidence's checkpoint must match the observation ledger version. An after-hours bootstrap advances that
    version without writing new evidence, which blocked the first live open (2026-10-14)."""
    from autonomous_trader.real_trading import RealGate
    RealGate(root,authority['account_binding']).checkpoint(authority['flat_checkpoint_sha256'])


def refresh_evidence_if_stale(root,clock=lambda:datetime.now(timezone.utc),sleep=readiness_sleep,bootstrap=None,max_wait_seconds=600,binding_check=None):
    """Re-measure deployment evidence automatically after a release changed the build.

    Read-only: scripts/real_bootstrap.py places no orders and never touches the owner charter, so a
    revoked, expired or tampered charter still fails load_authority afterwards. Needs a live session
    because the evidence is a measurement of real-time quotes."""
    try:
        authority=load_authority(root,clock())
        try:(binding_check or checkpoint_binding_check)(root,authority)
        except ValueError as exc:
            if str(exc)=='REAL_FLAT_CHECKPOINT_BINDING':raise
        except Exception:pass          # unreachable database etc.: run() reports it; not an evidence problem
        return 'CURRENT'
    except Exception as exc:
        if str(exc) not in STALE_EVIDENCE_REASONS:return 'NOT_REFRESHABLE'
    waited=0
    while True:
        now=clock();session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        if session is None or now>=session.closes_at:return 'CLOSED'
        if session.contains(now):break
        if waited>=max_wait_seconds:return 'MARKET_NOT_OPEN'
        sleep(15);waited+=15
    if bootstrap is None:
        from scripts.real_bootstrap import main as bootstrap
    try:return str(bootstrap().get('result','FAILED'))
    except Exception:return 'BOOTSTRAP_ERROR'


# Owner-approved 2026-10-16 ("approve auto retry"): a blocked start retries itself during the session, so a
# fix staged mid-day takes effect with nobody at the PC. Each retry is a fresh process running every gate again.
RETRY_MINUTES=15
NO_RETRY_CODES=frozenset({'REAL_DAILY_ALREADY_ACTIVE','REAL_RUNNER_ALREADY_ACTIVE'})


def retry_later(root,code,clock=lambda:datetime.now(timezone.utc),sleep=readiness_sleep,spawn=None):
    """Wait RETRY_MINUTES, then start a fresh daily run only if the session still permits entry and the owner
    authority still loads (revocation ends the chain)."""
    from datetime import timedelta
    if code in NO_RETRY_CODES:return 'NO_RETRY'
    deadline=clock()+timedelta(minutes=RETRY_MINUTES)
    while clock()<deadline:
        check_service_stop()
        sleep(min(60,max(1,(deadline-clock()).total_seconds())))
    now=clock()
    session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
    if not session or not session.permits_entry(now):return 'SESSION_OVER'
    try:load_authority(root,now)
    except Exception:return 'AUTHORITY_GONE'
    if spawn is None:
        flags=0
        if os.name=='nt':flags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS
        spawn=lambda argv:subprocess.Popen(argv,cwd=str(root),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,creationflags=flags,close_fds=True)
    spawn([sys.executable,'-I','-B',str(Path(root)/'scripts'/'run_real_daily.py')])
    return 'RETRY_STARTED'


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--prepare-only',action='store_true');parser.add_argument('--windows-service',action='store_true');parser.add_argument('--resident-worker',action='store_true');parser.add_argument('--stop-event');args=parser.parse_args()
    if args.windows_service:
        if args.prepare_only or args.resident_worker or args.stop_event:parser.error('service mode cannot combine worker or preparation arguments')
        from scripts.real_service_host import serve
        from scripts.service_worker import run_owned_worker
        return serve(run_owned_worker)
    if args.resident_worker:
        if args.prepare_only or not args.stop_event:parser.error('worker requires its stop event')
        from scripts.service_worker import StopEvent
        os.environ['PSModulePath']=str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/Modules')
        event=StopEvent(args.stop_event)
        try:resident(event)
        finally:event.close()
        return 0
    if args.stop_event:parser.error('stop event is only valid for resident worker')
    try:
        if not args.prepare_only:refresh_evidence_if_stale(ROOT)
        run(prepare_only=args.prepare_only,retry_readiness=not args.prepare_only)
    except Exception as exc:
        # The allowlist is deliberately narrow: uppercase provider text is not inherently safe.
        known={'REAL_DAILY_ALREADY_ACTIVE','REAL_RUNNER_ALREADY_ACTIVE','REAL_STORED_TOKEN_REQUIRED',
            'REAL_DAILY_UNRESOLVED_EXPOSURE_OR_ORDER','REAL_DAILY_UNRESOLVED_INCIDENT','REAL_DAILY_SAFETY_LATCH',
            'REAL_DAILY_CASH_MISMATCH','SETTLEMENT_CHANGE_UNRECONCILED','REAL_DAILY_SETTLEMENT_UNKNOWN',
            'REAL_DAILY_SUPERVISOR_BLOCKED','REAL_DAILY_START_EVIDENCE_EXPIRED'}
        code=str(exc) if type(exc) is ValueError and str(exc) in known else 'REAL_DAILY_PREPARATION_BLOCKED'
        # Invalid offline approval causes no runtime write, even a status file.
        try:load_authority(ROOT,datetime.now(timezone.utc))
        except Exception:print('REAL_PERSISTENT_AUTHORITY_REQUIRED');return 2
        blocked(ROOT,code,datetime.now(timezone.utc),exception_detail(exc));print(code)
        if not args.prepare_only:
            try:print(retry_later(ROOT,code))
            except Exception:pass
        return 2
    return 0


if __name__=='__main__':raise SystemExit(main())
