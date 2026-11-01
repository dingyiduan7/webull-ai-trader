"""Continuous MVP runner with independent read-only HTTP and heartbeat threads."""
import argparse,os,sys,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from autonomous_trader.mvp.config import RuntimeConfig
from autonomous_trader.durable.ledger import LocalDatabase,migrate,grant_writer
from autonomous_trader.monitoring.store import provision
from autonomous_trader.monitoring.runner import Engine
from autonomous_trader.monitoring.views import DashboardReader
from autonomous_trader.monitoring.server import DashboardServer

def parse_args(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--sme-advisory-file',type=Path)
    parser.add_argument('--sme-advisory-sha256')
    parser.add_argument('--sme-authorization-db',type=Path)
    parser.add_argument('--sme-advisory-provider',type=Path)
    parser.add_argument('--sme-advisory-provider-sha256')
    return parser.parse_args(argv)

def advisory_configuration(args):
    advisory=None
    if args.sme_advisory_provider or args.sme_advisory_provider_sha256:
        if any((args.sme_advisory_file,args.sme_advisory_sha256,args.sme_authorization_db)):return None
        from autonomous_trader.decision_api.sme_advisory import rotating_configuration
        return rotating_configuration(args.sme_advisory_provider,args.sme_advisory_provider_sha256)
    if args.sme_advisory_file and args.sme_advisory_sha256 and args.sme_authorization_db:
        from autonomous_trader.decision_api.sme_advisory import ApprovedSMEAdvisory
        advisory=ApprovedSMEAdvisory(args.sme_advisory_file,args.sme_advisory_sha256,args.sme_authorization_db)
    return advisory

def main():
    args=parse_args();advisory=advisory_configuration(args)
    config=RuntimeConfig.from_env();config.validate()
    schema='mvp_sandbox' if config.mode.value=='SANDBOX' else 'mvp_dry_run'
    admin=LocalDatabase('postgres','m2_admin',schema=schema)
    migrate(admin);grant_writer(admin,'m2_writer');store=provision(schema)
    lock=admin.connect();lock.autocommit=True
    if not lock.execute('SELECT pg_try_advisory_lock(17482026)').fetchone()[0]:raise ValueError('RUNNER_ALREADY_ACTIVE')
    engine=Engine(ROOT,config,store,interval=int(os.getenv('AI_TRADER_INTERVAL_SECONDS','300')),poll=int(os.getenv('AI_TRADER_POLL_SECONDS','30')),approved_sme_advisory=advisory)
    server=DashboardServer(DashboardReader(ROOT),args.port);stop=threading.Event()
    def pulse():
        while not stop.wait(3):
            try:lock.execute('SELECT 1');engine.heartbeat()
            except Exception:os._exit(1)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    threading.Thread(target=pulse,daemon=True).start()
    try:
        engine.heartbeat();print('AITrader dashboard: http://127.0.0.1:'+str(args.port),flush=True)
        engine.run(stop)
    except KeyboardInterrupt:pass
    finally:
        stop.set();engine.update(status='STOPPED',activity='Owner stopped runner')
        try:engine.heartbeat()
        except Exception:pass
        server.shutdown();server.server_close();lock.close()

if __name__=='__main__':
    try:main()
    except Exception:
        path=ROOT/'.local/mvp/startup-errors.log';path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a') as f:f.write('STARTUP_FAILED: check PostgreSQL, configuration, port and local secret availability.\n')
        print('AITrader startup failed; see .local/mvp/startup-errors.log',flush=True);sys.exit(1)
