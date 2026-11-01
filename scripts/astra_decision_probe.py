"""One Astra structured decision using live sandbox reads; zero order capability."""
from pathlib import Path
import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autonomous_trader.decision_api.astra_probe import load_openai_key, AstraClient, build_context
from autonomous_trader.sandbox.secrets import load_credentials, PreflightError
from autonomous_trader.sandbox.http import ReadOnlySandbox, Operation
from autonomous_trader.sandbox.sanitize import Sanitizer
from autonomous_trader.sandbox.evidence import save_json
from autonomous_trader.sandbox.quiet import quiet_sdk


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--key-file',help='Owner-selected local path; never the key value')
    args=parser.parse_args()
    if args.key_file:os.environ['OPENAI_API_KEY_FILE']=args.key_file
    report={'no_trade':True,'broker_order_calls':0,'decision_requests':0}
    key=load_openai_key()
    client=AstraClient(key,os.environ.get('OPENAI_PROJECT_ID'))
    credentials=load_credentials(ROOT)
    sanitizer=Sanitizer((*credentials.sensitive_values(),key))
    # Process-local one-shot transport, no tools or order adapter imported.
    try:
        report['model']=client.verify_model()
        broker=ReadOnlySandbox(credentials)
        observations={}
        with quiet_sdk():
            for name,op in (('accounts',Operation.ACCOUNTS),('balance',Operation.BALANCE),
                    ('positions',Operation.POSITIONS),('orders',Operation.OPEN_ORDERS),
                    ('instruments',Operation.INSTRUMENTS),('bars',Operation.HISTORY_BATCH),('snapshot',Operation.SNAPSHOT)):
                observations[name]=broker.read(op)
        context,registry=build_context(observations,credentials.account_id,sanitizer,datetime.now(timezone.utc))
        report['context']=context.model_dump(mode='json')
        # Only aliases and reviewed broker scalars are present in the model input.
        sanitizer.assert_clean(json.dumps(report['context']))
        result=client.decide(context,registry)
        report.update(result)
        report['decision_requests']=int(client._used)
    except PreflightError as exc:
        report['result']=str(exc)
    except Exception:
        report['result']='PROBE_STOPPED_DETAILS_SUPPRESSED'
    # Model-generated text is never an instruction to this runner.
    sanitizer.assert_clean(json.dumps(report))
    save_json(ROOT/'docs/astra-decision-result.json',report,sanitizer)
    print(json.dumps({k:report.get(k) for k in ('result','model','decision_requests','schema_valid',
        'semantics_valid','latency_ms','usage','estimated_cost_usd','no_trade')}))


if __name__=='__main__':
    try:main()
    except Exception:
        print('{"result":"PROBE_STOPPED_DETAILS_SUPPRESSED","no_trade":true}')
        sys.exit(1)
