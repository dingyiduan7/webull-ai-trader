from decimal import Decimal as D
from copy import deepcopy
from hashlib import sha256
import json
import pytest

import autonomous_trader.real_flat_checkpoint as subject
from autonomous_trader.real_flat_checkpoint import prove_flat_cash,validate_checkpoint


def fills():
    return [dict(quantity='0.05675',filled_price='16.82',fees_usd='.02'),
        dict(quantity='0.00188',filled_price='611.50',fees_usd='.02'),
        dict(quantity='0.00719',filled_price='167.30',fees_usd='.02'),
        dict(quantity='0.01733',filled_price='68.20',fees_usd='.02'),
        dict(quantity='0.01408',filled_price='68.40',fees_usd='.02')]


def test_sub_cent_precision_difference_is_proven_without_inventing_broker_net():
    rows=fills();net=sum((D(x['quantity'])*D(x['filled_price'])-D(x['fees_usd']) for x in rows),D(0))
    precise=D('500')+net+D('.00004')
    proof=prove_flat_cash(settled='500',unsettled=str((precise-D(500)).quantize(D('.01'))),
        cash_balance=str(precise.quantize(D('.01'))),equity=str(precise.quantize(D('.01'))),
        precise_cash=str(precise),fills=rows)
    assert proof['broker_net_fields']=='UNAVAILABLE'
    assert abs(D(proof['precision_difference_usd']))<D('.005')


@pytest.mark.parametrize('change',[dict(precise_cash='506'),dict(equity='505'),dict(unsettled='4.00')])
def test_actual_cash_or_equity_difference_is_not_excused_as_rounding(change):
    args=dict(settled='500',unsettled='5.35',cash_balance='505.35',equity='505.35',
        precise_cash='505.35',fills=fills());args.update(change)
    with pytest.raises(ValueError,match='UNSETTLED_PROCEEDS'):
        prove_flat_cash(**args)


def test_partial_broker_net_fields_are_rejected_not_inferred():
    rows=fills();rows[0]['broker_net_usd']='.93'
    with pytest.raises(ValueError,match='PARTIAL_BROKER_NET'):
        prove_flat_cash(settled='500',unsettled='5.35',cash_balance='505.35',equity='505.35',
            precise_cash='505.35',fills=rows)


def checkpoint():
    rows=fills();net=sum((D(x['quantity'])*D(x['filled_price'])-D(x['fees_usd']) for x in rows),D(0))
    precise=D('500')+net+D('.00004');unsettled=str((precise-D(500)).quantize(D('.01')))
    proof=prove_flat_cash(settled='500',unsettled=unsettled,cash_balance=str(precise.quantize(D('.01'))),
        equity=str(precise.quantize(D('.01'))),precise_cash=str(precise),fills=rows)
    return {'schema':'REAL_FLAT_CHECKPOINT_V1','environment':'REAL_READ_ONLY','observed_at':'2026-09-29T15:00:00+00:00',
        'account':{'type':'INDIVIDUAL_CASH','sole_account':True,'binding_matches_observation_journal':True},
        'positions_count':0,'open_order_count':0,'recent_orders':{'all_terminal':True,'ambiguous_count':0,'history_count':5},
        'cash':{'settled_usd':'500','unsettled_usd':unsettled,'cash_balance_usd':str(precise.quantize(D('.01'))),
            'precise_cash_evidence_usd':str(precise),'equity_usd':str(precise.quantize(D('.01'))),'totals_reconciled':True},
        'liquidation':{'fills':rows,'total_fees_usd':proof['fees_usd'],'precision_proof':proof},
        'local_integrity':{'observation_replay_verified':True,'observation_ledger_version':1,'writer_locks':0,
            'original_execution_approved':False,'recovery_execution_approved':False,
            'real_strategy_schema_present':False,'judicator_real_go_present':False},
        'authority':{'runner_started':False,'astra_called':False,'broker_mutations':0,'strategy_authority_enabled':False}}


def test_flat_checkpoint_validation_binds_cash_history_and_absence_of_authority():
    value=checkpoint();assert validate_checkpoint(value,account_binding='real-'+'a'*64) is value
    for mutate in ('cash','authority','position'):
        value=checkpoint()
        if mutate=='cash':value['cash']['unsettled_usd']='5.36'
        elif mutate=='authority':value['authority']['strategy_authority_enabled']=True
        else:value['positions_count']=1
        with pytest.raises(ValueError):validate_checkpoint(value,account_binding='real-'+'a'*64)


def v2_fixture(tmp_path,monkeypatch):
    folder=tmp_path/'.local'/'real-readiness';folder.mkdir(parents=True)
    binding='real-'+'a'*64
    artifacts={
        'base.json':checkpoint(),
        'account.json':{'schema':'REAL_ACCOUNT_CASH_MISMATCH_DIAGNOSTIC_V1','account_binding':binding,
            'flat_checkpoint_sha256':'placeholder','current_cash':{'settled_usd':'505.44',
                'unsettled_usd':'0.00','cash_balance_usd':'505.44','equity_usd':'505.44'},
            'account':{'flat':True,'positions_count':0,'open_nonfinal_order_count':0},
            'delta':{'cash_balance_usd':'0.09'},'authority':{'readiness_evidence':False}},
        'activity.json':{'schema':'REAL_CASH_ACTIVITY_DIAGNOSTIC_V1','account_binding':binding,
            'raw_exact_net_usd':'0','expected_checkpoint_delta_usd':'0.09',
            'net_matches_checkpoint_delta':False,'authority':{'readiness_evidence':False}}}
    def write(name,value):
        path=folder/name;path.write_text(json.dumps(value,indent=2),encoding='utf-8')
        return sha256(path.read_bytes()).hexdigest().upper()
    base_sha=write('base.json',artifacts['base.json'])
    artifacts['account.json']['flat_checkpoint_sha256']=base_sha
    account_sha=write('account.json',artifacts['account.json']);activity_sha=write('activity.json',artifacts['activity.json'])
    monkeypatch.setattr(subject,'BASE_CHECKPOINT_NAME','base.json');monkeypatch.setattr(subject,'BASE_CHECKPOINT_SHA256',base_sha)
    monkeypatch.setattr(subject,'ACCOUNT_DIAGNOSTIC_NAME','account.json');monkeypatch.setattr(subject,'ACCOUNT_DIAGNOSTIC_SHA256',account_sha)
    monkeypatch.setattr(subject,'CASH_ACTIVITY_DIAGNOSTIC_NAME','activity.json')
    monkeypatch.setattr(subject,'CASH_ACTIVITY_DIAGNOSTIC_SHA256',activity_sha)
    value={'schema':'REAL_FLAT_CHECKPOINT_V2','environment':'REAL_READ_ONLY',
        'observed_at':'2026-09-30T16:50:00+00:00','build_sha256':'b'*64,'account_binding':binding,
        'account':{'type':'INDIVIDUAL_CASH','sole_account':True,'binding_matches_observation_journal':True},
        'positions_count':0,'open_order_count':0,'recent_orders':{'history_count':5,'all_terminal':True,
            'ambiguous_count':0,'partial_or_unknown_count':0},
        'cash':{'settled_usd':'505.44','unsettled_usd':'0.00','cash_balance_usd':'505.44',
            'equity_usd':'505.44','totals_reconciled':True},
        'continuity':{'base_checkpoint':{'name':'base.json','sha256':base_sha},
            'account_diagnostic':{'name':'account.json','sha256':account_sha},
            'cash_activity_diagnostic':{'name':'activity.json','sha256':activity_sha},
            'base_total_usd':'505.35','current_total_usd':'505.44','adjustment_usd':'0.09',
            'adjustment_type':'UNATTRIBUTED_NON_TRADING_BROKER_ADJUSTMENT','cash_activity_net_usd':'0',
            'cash_activity_match':False,'trading_profit_usd':'0','realized_pnl_effect_usd':'0'},
        'owner_authorization':{'scope':'REAL_FLAT_CHECKPOINT_V2_SUPERSESSION','decision':'AUTHORIZED',
            'provenance':'OWNER_VIA_PROJECT_COMMAND_CENTER'},
        'local_integrity':{'observation_replay_verified':True,'observation_ledger_version':1,
            'writer_locks':0,'real_strategy_schema_present':False,'judicator_real_go_present':False},
        'authority':False}
    return value,binding,folder


def test_v2_binds_predecessor_diagnostics_cash_and_zero_profit(tmp_path,monkeypatch):
    value,binding,_=v2_fixture(tmp_path,monkeypatch)
    assert validate_checkpoint(value,account_binding=binding,root=tmp_path) is value


@pytest.mark.parametrize(('path','replacement','error'),[
    (('cash','settled_usd'),'505.45','V2_CASH'),
    (('continuity','adjustment_type'),'BROKER_ADJUSTMENT','V2_CONTINUITY'),
    (('continuity','trading_profit_usd'),'0.09','V2_CONTINUITY'),
    (('continuity','realized_pnl_effect_usd'),'0.09','V2_CONTINUITY'),
    (('authority',),True,'V2_AUTHORITY'),
    (('account_binding',),'real-'+'b'*64,'V2_SCHEMA')])
def test_v2_rejects_authority_profit_or_wrong_continuity(tmp_path,monkeypatch,path,replacement,error):
    value,binding,_=v2_fixture(tmp_path,monkeypatch);value=deepcopy(value)
    target=value
    for key in path[:-1]:target=target[key]
    target[path[-1]]=replacement
    with pytest.raises(ValueError,match=error):validate_checkpoint(value,account_binding=binding,root=tmp_path)


@pytest.mark.parametrize('artifact',['base.json','account.json','activity.json'])
def test_v2_rejects_missing_or_tampered_bound_artifact(tmp_path,monkeypatch,artifact):
    value,binding,folder=v2_fixture(tmp_path,monkeypatch)
    (folder/artifact).write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='V2_ARTIFACT'):
        validate_checkpoint(value,account_binding=binding,root=tmp_path)
