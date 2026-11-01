"""Initialize Webull REAL 2FA token and store it with Windows DPAPI. No trading APIs."""
from pathlib import Path
import json, os, subprocess, sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from webull.core.client import ApiClient
from webull.core.http.initializer.client_initializer import ClientInitializer
from webull.core.http.initializer.token.token_manager import TokenManager
from autonomous_trader.real_preflight import REAL_HTTP_HOST,RealReadOnly,Operation,require_success,select_single_individual_cash
from autonomous_trader.real_secrets import RealCredentials,load_real_credentials
from autonomous_trader.sandbox.quiet import quiet_sdk


def plaintext(path):
    labels={}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        delimiter=':' if ':' in line else '=' if '=' in line else None
        if delimiter:
            key,value=line.split(delimiter,1);labels[key.strip().lower()]=value.strip()
    key=labels.get('real app key','');secret=labels.get('real app sec','')
    if not key or not secret:raise ValueError('REAL_CREDENTIAL_FILE_SCHEMA')
    return key,secret


def save(key,secret,account_id,token):
    command="""$ErrorActionPreference='Stop';$v=$input|Out-String|ConvertFrom-Json;
    $out=@{AppKey=(ConvertTo-SecureString $v.AppKey -AsPlainText -Force);
    AppSecret=(ConvertTo-SecureString $v.AppSecret -AsPlainText -Force);
    AccountId=(ConvertTo-SecureString $v.AccountId -AsPlainText -Force);
    AccessToken=(ConvertTo-SecureString $v.AccessToken -AsPlainText -Force)};
    $out|Export-Clixml -LiteralPath $env:AITRADER_REAL_DPAPI_PATH -Force"""
    path=ROOT/'.secrets'/'webull-real.clixml';path.parent.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,AITRADER_REAL_DPAPI_PATH=str(path))
    body=json.dumps({'AppKey':key,'AppSecret':secret,'AccountId':account_id,'AccessToken':token}).encode()
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],input=body,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise ValueError('REAL_DPAPI_WRITE_FAILED')
    stored=load_real_credentials(ROOT)
    if (stored.app_key,stored.app_secret,stored.account_id,stored.access_token)!=(key,secret,account_id,token):
        raise ValueError('REAL_DPAPI_VERIFY_FAILED')


@quiet_sdk()
def main(path=None):
    # Default: the DPAPI-encrypted keys stored by set_webull_real_secrets.ps1.
    # A plaintext credential file is accepted only for legacy setups.
    if path is None:
        stored=load_real_credentials(ROOT);key,secret=stored.app_key,stored.app_secret
    else:
        key,secret=plaintext(path)
    api=ApiClient(key,secret,'us',verify=True,auto_retry=False,connect_timeout=5,timeout=10,
        token_check_duration_seconds=300,token_check_interval_seconds=5)
    api.add_endpoint('us',REAL_HTTP_HOST)
    enabled=ClientInitializer._check_token_enable(api)
    token=''
    if enabled:
        value=TokenManager().fetch_token_from_server(api,None)
        if value.get('status')!='NORMAL' or not value.get('token'):raise ValueError('REAL_TOKEN_NOT_VERIFIED')
        token=value['token'];api.set_token(token)
    credentials=RealCredentials(key,secret,access_token=token)
    reader=RealReadOnly(credentials)
    observation=require_success(Operation.ACCOUNTS,reader.read(Operation.ACCOUNTS))
    account=select_single_individual_cash(observation.body)
    save(key,secret,account['account_id'],token)
    print(json.dumps({'status':'REAL_CREDENTIALS_DPAPI_STORED','token_required':enabled,
        'account_type':'INDIVIDUAL_CASH','host':REAL_HTTP_HOST}))


if __name__=='__main__':
    if len(sys.argv)>2:raise SystemExit('usage: webull_real_token_setup.py [legacy-credential-file]')
    print('Read-only: verifies the Webull keys, completes 2-step token approval if your account requires it '
          '(watch the Webull app for an approval prompt), and confirms the cash account. No orders.',flush=True)
    try:
        main(sys.argv[1] if len(sys.argv)==2 else None)
    except Exception as exc:
        code=str(exc) if isinstance(exc,ValueError) and str(exc).isupper() else type(exc).__name__
        print(json.dumps({'status':'REAL_CREDENTIAL_SETUP_FAILED','reason':code}));raise SystemExit(2)
