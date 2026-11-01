"""One configured cycle; no scheduler, no production enablement, no trade quota."""
from pathlib import Path
import os,sys,json,argparse
from datetime import datetime,timezone
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autonomous_trader.mvp.config import RuntimeConfig,ExecutionMode,reviewed_calendar
from autonomous_trader.mvp.context import SandboxSource,make_context,INSTRUCTIONS
from autonomous_trader.mvp.gateway import ExecutionGateway,SandboxTransport,LocalPositionManager
from autonomous_trader.mvp.bootstrap import prepare_controller
from autonomous_trader.mvp.cycle import run_trading_cycle
from autonomous_trader.decision_api.brain import load_model_key,make_brain
from autonomous_trader.sandbox.secrets import load_credentials
from autonomous_trader.sandbox.certification.sanitize import OrderSanitizer
from autonomous_trader.sandbox.evidence import save_json
from autonomous_trader.execution import SimulatedBroker
from autonomous_trader.durable.ledger import Ledger,LocalDatabase,migrate,grant_writer

WRITER_LOCK=17482026


def acquire_writer_lock(admin):
    lock=admin.connect();lock.autocommit=True
    if not lock.execute('SELECT pg_try_advisory_lock(%s)',(WRITER_LOCK,)).fetchone()[0]:
        lock.close();raise ValueError('RUNNER_ALREADY_ACTIVE')
    return lock


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--key-file');parser.add_argument('--analyze-closed',action='store_true')
    args=parser.parse_args()
    config=RuntimeConfig.from_env();config.validate()  # production blocked before credentials/network
    if args.key_file:os.environ['ANTHROPIC_API_KEY_FILE' if config.model.startswith('claude-') else 'OPENAI_API_KEY_FILE']=args.key_file
    clock=lambda:datetime.now(timezone.utc)
    calendar=reviewed_calendar();now=clock()
    session=calendar.session(now.astimezone(__import__('zoneinfo').ZoneInfo('America/New_York')).date())
    report={'mode':config.mode.value,'order_submitted':False,'risk_approved':False,'session_gate':'CLOSED_OR_ENTRY_CUTOFF',
        'model':config.model,'reasoning':config.reasoning,'source':'REAL_MODEL_NOT_SYNTHETIC'}
    if (session is None or not session.permits_entry(now)) and not args.analyze_closed:
        print(json.dumps(report));return
    lock=None;sanitizer=OrderSanitizer(())
    if session is not None and session.permits_entry(now):
        admin=LocalDatabase('postgres','m2_admin',schema='mvp_sandbox' if config.mode==ExecutionMode.SANDBOX else 'mvp_dry_run')
        lock=acquire_writer_lock(admin)
    try:
        credentials=load_credentials(ROOT);key=load_model_key(config.model)
        sanitizer=OrderSanitizer((*credentials.sensitive_values(),key))
        brain=make_brain(key,model=config.model,reasoning=config.reasoning,instructions=INSTRUCTIONS)
        source=SandboxSource(credentials)
        brain.verify_model()
        if session is None or not session.permits_entry(clock()):
            o=source.refresh()
            context,registry=make_context(o,None,clock(),analysis_only=True)
            ai=brain.decide(context,registry,analysis_only=True)
            report.update(ai=ai,astra_action=(ai.get('decision') or {}).get('action'),context=context.model_dump(mode='json'),
                blocker='SESSION_CLOSED_EXECUTION_BLOCKED')
        else:
            o=source.refresh();o.require_fresh(clock())
            schema='mvp_sandbox' if config.mode==ExecutionMode.SANDBOX else 'mvp_dry_run'
            migrate(admin);grant_writer(admin,'m2_writer')
            ledger=Ledger(LocalDatabase('postgres','m2_writer',schema=schema))
            controller=prepare_controller(ledger,config,o,credentials.account_id,clock)
            def capture(value):
                save_json(ROOT/'.local/mvp/evidence'/(uuid4().hex+'.json'),value,sanitizer)
            gateway=ExecutionGateway(config,simulation=SimulatedBroker(calendar) if config.mode==ExecutionMode.DRY_RUN else None,
                transport=SandboxTransport(credentials) if config.mode==ExecutionMode.SANDBOX else None,
                sanitizer=sanitizer,evidence_sink=capture)
            report.update(run_trading_cycle(config=config,calendar=calendar,clock=clock,source=source,brain=brain,
                controller=controller,gateway=gateway,context_factory=make_context,monitor=LocalPositionManager()))
    except Exception as exc:
        import re
        reason=str(exc)
        report['blocker']=reason if re.fullmatch('[A-Z_ ,]{1,150}',reason) else 'MVP_STOPPED_DETAILS_SUPPRESSED'
    finally:
        if lock is not None:lock.close()
    sanitizer.assert_clean(json.dumps(report))
    save_json(ROOT/'docs/mvp-real-cycle.json',report,sanitizer)
    print(json.dumps({k:report.get(k) for k in ('astra_action','risk_approved','order_submitted','broker_status','session_gate','blocker')}))


if __name__=='__main__':
    try:main()
    except Exception:
        print('{"blocker":"CONFIGURATION_OR_SECRET_UNAVAILABLE","order_submitted":false}');sys.exit(1)
