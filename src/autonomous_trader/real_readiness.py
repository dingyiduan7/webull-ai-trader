"""Sanitized REAL observations only. No order, model, ledger or runner authority."""
from .runtime_storage import data_root
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Literal
import json
import os

from pydantic import BaseModel, ConfigDict, Field, AwareDatetime


class ExternalHolding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str = Field(pattern=r'^[A-Z][A-Z0-9.]{0,9}$')
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    market_value_usd: Decimal = Field(allow_inf_nan=False)
    last_price_usd: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    ownership: Literal['EXTERNAL'] = 'EXTERNAL'
    automation_authority: Literal['NONE'] = 'NONE'


class RealReadiness(BaseModel):
    model_config = ConfigDict(extra='forbid')
    environment: Literal['REAL_READ_ONLY'] = 'REAL_READ_ONLY'
    observed_at: AwareDatetime
    account_type: Literal['INDIVIDUAL_CASH'] = 'INDIVIDUAL_CASH'
    settled_usd: Decimal = Field(ge=0, allow_inf_nan=False)
    unsettled_usd: Decimal = Field(allow_inf_nan=False)
    equity_usd: Decimal = Field(ge=0, allow_inf_nan=False)
    external_holdings: tuple[ExternalHolding, ...]
    open_order_count: int = Field(ge=0)
    account_totals_match: bool
    display_rounding_difference_usd: Decimal = Decimal(0)
    display_rounding_proven: bool = False
    broker_market_value_usd: Decimal | None = None
    observation_ledger_version: int | None = None
    observation_reconciled: bool = False
    observation_incident_latched: bool | None = None
    market_data: Literal['NOT_CHECKED', 'ENTITLEMENT_BLOCKED', 'OBSERVED_CURRENT_SAMPLE',
                         'OBSERVED_DELAYED_OR_STALE_SAMPLE', 'FUTURE_TIMESTAMP_INVALID'] = 'NOT_CHECKED'
    production_enabled: Literal[False] = False
    unattended_overnight_enabled: Literal[False] = False
    reconciliation_ready: Literal[False] = False
    # Broker observations are not a durable REAL ledger or latch initialization.
    latches: Literal['NOT_INITIALIZED'] = 'NOT_INITIALIZED'


def account_readiness(balance, positions, orders, at):
    assets = balance.get('account_currency_assets', [])
    if len(assets) != 1 or assets[0].get('currency') != 'USD':
        raise ValueError('REAL_USD_CASH_SHAPE')
    cash = assets[0]
    holdings = tuple(ExternalHolding(symbol=row['symbol'], quantity=row['quantity'],
        market_value_usd=row['market_value'], last_price_usd=row.get('last_price'))
        for row in positions if Decimal(row['quantity']) != 0)
    if len({row.symbol for row in holdings}) != len(holdings):
        raise ValueError('REAL_DUPLICATE_HOLDING')
    if not isinstance(orders.get('data'), list) or orders.get('pagination_key'):
        raise ValueError('REAL_OPEN_ORDER_PAGINATION_UNRESOLVED')
    settled, unsettled, equity = (Decimal(cash[key]) for key in
        ('settled_cash', 'unsettled_cash', 'net_liquidation_value'))
    # Equality is observational only; never manufacture an accounting adjustment.
    display_sum = sum((p.market_value_usd for p in holdings), Decimal(0))
    broker_value = Decimal(cash['market_value']) if 'market_value' in cash else None
    rounded = lambda value: value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    rounding_proven = (broker_value is not None and all(p.last_price_usd is not None and
        rounded(p.quantity*p.last_price_usd) == p.market_value_usd for p in holdings) and
        rounded(sum((p.quantity*p.last_price_usd for p in holdings), Decimal(0))) == broker_value)
    matched = (settled + unsettled == Decimal(cash['cash_balance']) and
        (display_sum == equity-Decimal(cash['cash_balance']) or
         (rounding_proven and broker_value == equity-Decimal(cash['cash_balance']))))
    return RealReadiness(observed_at=at, settled_usd=settled, unsettled_usd=unsettled,
        equity_usd=equity, external_holdings=holdings, open_order_count=len(orders['data']),
        account_totals_match=matched, broker_market_value_usd=broker_value,
        display_rounding_proven=rounding_proven,
        display_rounding_difference_usd=Decimal(0) if broker_value is None else broker_value-display_sum)


def evidence_path(root):
    return data_root(root) / '.local' / 'real-readiness' / 'observation.json'


def save_readiness(root, observation, sanitizer):
    # Revalidate copies too: model_copy is not a validation boundary.
    value = RealReadiness.model_validate(vars(observation))
    text = value.model_dump_json(indent=2)
    sanitizer.assert_clean(text)
    path = evidence_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def dashboard_readiness(root, now=None):
    from .real_liquidation import dashboard_status
    from .durable.ledger import LocalDatabase
    from .durable.projection import MAX_ENTRY_SUBMISSIONS_PER_SESSION, MAX_ENTRY_SUBMISSIONS_PER_EXCHANGE_WEEK
    from .monitoring.model_budget import (REAL_MONTHLY_COST_CONTROL,REAL_MONTHLY_COST_SUSPENSION_ID,
                                         MAX_PORTFOLIO_CALLS_PER_SESSION,
                                         MAX_PORTFOLIO_CALLS_PER_EXCHANGE_WEEK,
                                         MAX_POSITION_CALLS_PER_SESSION,
                                         MAX_POSITION_CALLS_PER_EXCHANGE_WEEK,
                                         MAX_REAL_CALLS_PER_SESSION,
                                         MAX_REAL_CALLS_PER_EXCHANGE_WEEK)
    now = now or datetime.now(timezone.utc)
    result = {'environment': 'REAL_READ_ONLY', 'production_enabled': False,
        'liquidation': dashboard_status(LocalDatabase('postgres', 'm2_admin', schema='real_readiness')),
        'unattended_overnight_enabled': False, 'reconciliation_ready': False,
        'latches': 'NOT_INITIALIZED', 'status': 'NO_OBSERVATION',
        'entries_per_session': None,
        'round_trips_per_exchange_week': None,
        'trade_limit_control': 'RESERVED_ENTRY_EXIT_ROUND_TRIP_V1',
        'portfolio_calls_per_session': None,
        'portfolio_calls_per_week': None,
        'position_calls_per_session': None,
        'position_calls_per_week': None,
        'astra_calls_per_session': MAX_REAL_CALLS_PER_SESSION,
        'astra_calls_per_week': MAX_REAL_CALLS_PER_EXCHANGE_WEEK,
        'monthly_hard_cap_usd': None,
        'monthly_model_cost_control': REAL_MONTHLY_COST_CONTROL,
        'monthly_model_cost_suspension_id': REAL_MONTHLY_COST_SUSPENSION_ID,
        'real_model_spend_usd': None, 'real_quota_usage': None,
        'authority_blocker': 'JUDICATOR_REAL_GO_REQUIRED'}
    from .real_flat_checkpoint import V2_FILENAME
    checkpoint=data_root(root)/'.local'/'real-readiness'/V2_FILENAME
    if checkpoint.is_file():
        try:
            from .real_flat_checkpoint import load_checkpoint
            from .real_state import RealObservationLedger
            db=LocalDatabase('postgres','m2_admin',schema='real_readiness')
            state=RealObservationLedger(db).verify_replay()
            value,digest=load_checkpoint(checkpoint,account_binding=state['account_binding'])
            cash=value['cash'];local=value['local_integrity']
            result.update(status='FLAT_CHECKPOINT_VERIFIED',observed_at=value['observed_at'],market_data='NOT_CHECKED',
                settled_usd=cash['settled_usd'],unsettled_usd=cash['unsettled_usd'],equity_usd=cash['equity_usd'],
                open_order_count=0,account_totals_match=True,observation_reconciled=True,
                observation_ledger_version=local['observation_ledger_version'],observation_incident_latched=False,
                external_holdings=[],flat_checkpoint_sha256=digest,flat_checkpoint_verified=True)
            packets=sorted((data_root(root)/'.local'/'real-readiness').glob('after-hours-account-packet-*.json'))
            if packets:
                packet=json.loads(packets[-1].read_text(encoding='utf-8'));account=packet.get('account',{})
                cash=packet.get('cash',{});authority=packet.get('authority',{})
                if (packet.get('schema')=='REAL_AFTER_HOURS_ACCOUNT_PACKET_V1'
                        and packet.get('flat_checkpoint_sha256')==digest and account.get('flat') is True
                        and account.get('positions_count')==0 and account.get('open_nonfinal_order_count')==0
                        and authority.get('production_enabled') is False and authority.get('broker_mutations')==0):
                    result.update(status='REAL_DISABLED_NO_GO',observed_at=packet['created_at'],
                        settled_usd=cash['settled_usd'],unsettled_usd=cash['unsettled_usd'],equity_usd=cash['equity_usd'],
                        open_order_count=0,external_holdings=[],market_data='DEFERRED_TO_CORE_SESSION',
                        settlement_status=cash.get('continuity'),current_evidence='AUTHENTICATED_AFTER_HOURS_PACKET',
                        historical_observation_preserved=True)
            if (data_root(root)/'.local'/'real-readiness'/'judicator-real-go.json').is_file():
                try:
                    from .real_trading import RealGate
                    receipt=RealGate.offline_receipt(root,now)
                    result.update(status='TECHNICAL_GO_PRESENT_CURRENT_RECONCILIATION_REQUIRED',
                        authority_blocker='CURRENT_REAL_RECONCILIATION_REQUIRED',
                        expected_build_sha256=receipt['build_sha256'],
                        expected_market_session=receipt['market_session_label'])
                except ValueError as exc:
                    result.update(status='REAL_DISABLED_INVALID_OR_STALE_GO',authority_blocker=str(exc))
            return result
        except Exception:
            result.update(status='FLAT_CHECKPOINT_INVALID',authority_blocker='FLAT_CHECKPOINT_REVIEW_REQUIRED')
            return result
    # Display only (never authority): newest bootstrap snapshot, so equity/cash show before the first live ledger exists.
    try:
        folder=data_root(root)/'.local'/'real-readiness'
        latest=sorted(folder.glob('flat-checkpoint-v3-*.json'))[-1]
        snap=json.loads(latest.read_text(encoding='utf-8'));cash=snap['cash']
        if (snap.get('schema')=='REAL_FLAT_CHECKPOINT_V3' and snap.get('positions_count')==0
                and snap.get('open_order_count')==0 and cash.get('totals_reconciled') is True):
            result.update(status='FLAT_SNAPSHOT_OBSERVED',observed_at=snap['observed_at'],market_data='NOT_CHECKED',
                settled_usd=cash['settled_usd'],unsettled_usd=cash['unsettled_usd'],equity_usd=cash['equity_usd'],
                open_order_count=0,account_totals_match=True,flat_checkpoint_verified=False,external_holdings=[])
            return result
    except Exception:
        pass
    try:
        value = RealReadiness.model_validate_json(evidence_path(root).read_text(encoding='utf-8'))
        age = (now - value.observed_at).total_seconds()
        result.update(value.model_dump(mode='json'))
        result.update(status='RECENT_READ_ONLY_OBSERVATION' if 0 <= age <= 30 else 'STALE_OBSERVATION',
                      observation_age_seconds=age)
        if not 0 <= age <= 30:
            result['observation_reconciled'] = False
    except (OSError, ValueError):
        pass
    return result
