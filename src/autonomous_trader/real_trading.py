"""REAL cash composition boundary. No activation receipt is created by this code."""
from .recovery_reasons import FLAT_RECOVERABLE_REASONS,HELD_RECOVERABLE_REASONS
from .runtime_storage import data_root
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date, timezone
from decimal import Decimal
from hashlib import sha256
from importlib.metadata import version as distribution_version, PackageNotFoundError
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

from .contracts import Instrument, CapabilityManifest, AlphaContext, OrderIntent
from .durable.ledger import Ledger, LocalDatabase
from .durable.models import (CreateAccount, OpeningReceivable, Mark, RegisterInstruments,
    Reconcile, ConfirmSettlement, NewSession, Admit)
from .durable.projection import Rejected, settlement_observation_date
from .mvp.config import reviewed_calendar
from .mvp.controller import Controller
from .reference import NEW_YORK
from .real_state import digest
from .real_flat_checkpoint import load_checkpoint

CAPABILITY_VERSION='real-cash-local-stop-v1'
SOURCE='WEBULL_REAL_READ_ONLY'
REALTIME_ENTITLEMENT_DOCS={
    'MARKET_DATA_OVERVIEW':'https://developer.webull.com/apis/docs/market-data-api/overview/',
    'MARKET_DATA_GETTING_STARTED':'https://developer.webull.com/apis/docs/market-data-api/getting-started/',
    'DATA_STREAMING_API':'https://developer.webull.com/apis/docs/market-data-api/data-streaming-api/',
    'MARKET_DATA_FAQ':'https://developer.webull.com/apis/docs/market-data-api/faq/',
}


LIVE_ENTITLEMENT_PRODUCT='WEBULL_OPENAPI_REALTIME_OBSERVED'
LIVE_QUOTE_MAX_AGE_MS=10000


def _validate_live_entitlement(value,prepare,now):
    """Entitlement proven by measurement (scripts/real_bootstrap.py): during a regular
    session, production quotes for liquid Nasdaq names arrived <= 10 s after their
    exchange timestamp. Delayed data is ~15 minutes old and cannot pass. The daily
    certificate re-measures freshness every session (validate_coverage)."""
    required={'account_binding','build_sha256','session_label','status','product','observed_at','source_evidence'}
    try:
        observed=datetime.fromisoformat(value['observed_at']);source=value['source_evidence']
        samples=source['samples']
        if (set(value)!=required or value['account_binding']!=prepare['account_binding']
                or value['build_sha256']!=prepare['build_sha256']
                or value['session_label']!=prepare['market_session_label']
                or value['status']!='VERIFIED' or observed.tzinfo is None or observed>now
                or set(source)!={'kind','environment','http_host','max_quote_age_ms','samples'}
                or source['kind']!='LIVE_QUOTE_FRESHNESS' or source['environment']!='PRODUCTION'
                or source['http_host']!='api.webull.com' or source['max_quote_age_ms']!=LIVE_QUOTE_MAX_AGE_MS
                or not isinstance(samples,list) or len(samples)<2):
            raise ValueError()
        for sample in samples:
            quote_at=datetime.fromisoformat(sample['quote_at']);received=datetime.fromisoformat(sample['received_at'])
            if (set(sample)!={'symbol','quote_at','received_at','age_ms','delay_evidence'}
                    or quote_at.tzinfo is None or received.tzinfo is None
                    or not 0<=(received-quote_at).total_seconds()*1000<=LIVE_QUOTE_MAX_AGE_MS
                    or sample['age_ms']!=round((received-quote_at).total_seconds()*1000)
                    or received>observed+timedelta(seconds=60)
                    or sample['delay_evidence'] not in ('EXPLICIT_RESPONSE',
                        'DOCUMENTED_PRODUCTION_OPENAPI_SUBSCRIPTION_PLUS_CURRENT_EFFECTIVENESS')):
                raise ValueError()
    except (KeyError,TypeError,ValueError,AttributeError):raise ValueError('REAL_ENTITLEMENT_UNVERIFIED') from None
    return value


def validate_realtime_entitlement(value,prepare,now,root):
    """Validate current production OpenAPI entitlement evidence without broker access."""
    if isinstance(value,dict) and value.get('product')==LIVE_ENTITLEMENT_PRODUCT:
        return _validate_live_entitlement(value,prepare,now)
    required={'account_binding','build_sha256','session_label','status','product','observed_at','source_evidence'}
    try:
        observed=datetime.fromisoformat(value['observed_at'])
        source=value['source_evidence'];expiration=date.fromisoformat(source['displayed_expiration'])
    except (KeyError,TypeError,ValueError):raise ValueError('REAL_ENTITLEMENT_UNVERIFIED') from None
    product=value.get('product');provider=source.get('provider_response') if isinstance(source,dict) else None
    required_source={'kind','source','environment','http_host','non_display','enabled','product','provider_response',
        'displayed_authorization','displayed_expiration','screenshot_sha256','screenshot_path','official_documentation'}
    base=(data_root(root)/'.local'/'real-readiness').resolve()
    screenshot=(data_root(root)/str(source.get('screenshot_path',''))).resolve()
    screenshot_valid=(screenshot!=base and base in screenshot.parents and screenshot.is_file()
        and sha256(screenshot.read_bytes()).hexdigest().upper()==str(source.get('screenshot_sha256','')).upper())
    if (set(value)!=required or value['account_binding']!=prepare['account_binding']
            or value['build_sha256']!=prepare['build_sha256'] or value['session_label']!=prepare['market_session_label']
            or value['status']!='VERIFIED' or product not in ('NASDAQ_BASIC_OPENAPI_NONDISPLAY','NASDAQ_TOTALVIEW_OPENAPI_NONDISPLAY')
            or observed.tzinfo is None or observed>now
            or not isinstance(source,dict) or set(source)!=required_source
            or source.get('kind')!='WEBULL_OPENAPI_ENTITLEMENT'
            or source.get('source')!='WEBULL_DEVELOPER_PORTAL_AUTHENTICATED'
            or source.get('environment')!='PRODUCTION' or source.get('http_host')!='api.webull.com'
            or source.get('non_display') is not True or source.get('enabled') is not True
            or source.get('product')!=product or source.get('displayed_authorization')!='Free Authorized'
            or expiration<now.date() or source.get('official_documentation')!=REALTIME_ENTITLEMENT_DOCS
            or not re.fullmatch(r'[0-9A-F]{64}',str(source.get('screenshot_sha256','')))
            or not isinstance(source.get('screenshot_path'),str) or not source['screenshot_path'].startswith('.local/real-readiness/')
            or not screenshot_valid
            or provider!={'subscription_name':product,'market':'NASDAQ','usage':'NON_DISPLAY_OPENAPI','status':'ACTIVE'}):
        raise ValueError('REAL_ENTITLEMENT_UNVERIFIED')
    return value
def build_digest(root):
    root=Path(root);base=root/'src'/'autonomous_trader'
    values=[(str(path.relative_to(base)),sha256(path.read_bytes()).hexdigest())
            for path in sorted(base.rglob('*.py'))]
    authority_files=(base/'durable'/'schema.sql',base/'alpha-decision.schema.json',
        base/'trade-decision.schema.json',base/'mvp'/'strategy.json',
        root/'alpha-decision.schema.json',root/'trade-decision.schema.json',
        root/'pyproject.toml',root/'requirements.txt')
    for path in authority_files:
        if not path.is_file():raise ValueError('REAL_RUNTIME_AUTHORITY_ARTIFACT_REQUIRED')
        values.append((str(path.relative_to(root)),sha256(path.read_bytes()).hexdigest()))
    storage=root/'runtime-storage.json'
    if storage.exists():
        data_root(root)  # Validate before binding the configuration.
        values.append(('runtime-storage.json',sha256(storage.read_bytes()).hexdigest()))
        native=root/'vendor/postgresql'
        for name in ('pg_ctl.exe','postgres.exe','initdb.exe'):
            if not (native/'bin'/name).is_file():raise ValueError('REAL_PROTECTED_DATABASE_BINARY_REQUIRED')
        for path in sorted(native.rglob('*')):
            if path.is_symlink():raise ValueError('REAL_PROTECTED_DATABASE_LINK')
            if path.is_file():values.append((str(path.relative_to(root)),sha256(path.read_bytes()).hexdigest()))
    scripts=root/'scripts'
    if not scripts.is_dir():raise ValueError('REAL_LAUNCHER_REQUIRED')
    executables=tuple(sorted(path for suffix in ('*.py','*.ps1','*.cmd') for path in scripts.glob(suffix)))
    for launcher in executables:
        values.append((str(launcher.relative_to(root)),sha256(launcher.read_bytes()).hexdigest()))
    required_scripts={'run_real_trader.py','run_real_supervisor.py','run_ai_trader.py','run_trading_cycle.py',
        'webull_real_readiness_packet.py','webull_real_after_hours_account_packet.py'}
    if not required_scripts.issubset({path.name for path in scripts.glob('*.py')}):raise ValueError('REAL_LAUNCHER_REQUIRED')
    expected={}
    for line in (root/'requirements.txt').read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        if line.count('==')!=1:raise ValueError('REAL_RUNTIME_MANIFEST_NOT_FULLY_PINNED')
        name,version=line.split('==');normalized=re.sub(r'[-_.]+','-',name.strip()).lower()
        if not normalized or not version.strip() or normalized in expected:
            raise ValueError('REAL_RUNTIME_MANIFEST_INVALID')
        expected[normalized]=(name.strip(),version.strip())
    if 'webull-openapi-python-sdk' not in expected:raise ValueError('REAL_RUNTIME_MANIFEST_INVALID')
    try:runtime={normalized:distribution_version(name) for normalized,(name,_) in expected.items()}
    except PackageNotFoundError:raise ValueError('REAL_RUNTIME_DISTRIBUTION_REQUIRED') from None
    if runtime!={normalized:version for normalized,(_,version) in expected.items()}:
        raise ValueError('REAL_RUNTIME_DISTRIBUTION_MISMATCH')
    values.append(('runtime-environment',digest({'python':sys.version,'packages':runtime})))
    return digest(values)


@dataclass(frozen=True)
class RealPacket:
    account_binding: str
    observed_at: datetime
    settled: Decimal
    unsettled: Decimal
    equity: Decimal
    positions: dict
    open_client_ids: tuple
    instruments: tuple[Instrument,...]
    context: AlphaContext
    evidence_id: str
    quote_received_at: dict[str,datetime]
    market_data_evidence: dict=field(default_factory=dict)
    host: str='api.webull.com'
    account_type: str='INDIVIDUAL_CASH'

    def validate(self,now,*,flat=False,entry=False):
        if self.host!='api.webull.com' or self.account_type!='INDIVIDUAL_CASH':raise ValueError('REAL_CASH_ACCOUNT_REQUIRED')
        if not self.account_binding.startswith('real-'):raise ValueError('REAL_ACCOUNT_BINDING_REQUIRED')
        if not 0<=(now-self.observed_at).total_seconds()<=10:raise ValueError('STALE_REAL_ACCOUNT')
        session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        if session is None or not session.contains(now):raise ValueError('REGULAR_SESSION_REQUIRED')
        if flat and (self.positions or self.open_client_ids):raise ValueError('REAL_FLAT_ZERO_ORDERS_REQUIRED')
        if self.settled<0 or self.unsettled<0:raise ValueError('REAL_CASH_INVALID')
        if not self.context.session_open==session.opens_at or self.context.session_close!=session.closes_at:
            raise ValueError('PRIOR_SESSION_CONTEXT')
        if not self.context.as_of<=now<self.context.expires_at:raise ValueError('STALE_REAL_CONTEXT')
        refs={i.instrument_id:i for i in self.instruments}
        quotes={q.instrument_id:q for q in self.context.instruments}
        if len(refs)!=len(self.instruments) or len(quotes)!=len(self.context.instruments):raise ValueError('DUPLICATE_REAL_INSTRUMENT')
        if set(self.quote_received_at)!=set(quotes):raise ValueError('REAL_QUOTE_RECEIPT_BINDING')
        if set(self.positions)-set(quotes):raise ValueError('HELD_REAL_CONTEXT_MISSING')
        for iid,q in quotes.items():
            if iid not in refs or refs[iid].ticker!=q.symbol:raise ValueError('REAL_INSTRUMENT_BINDING')
            if not session.opens_at<=q.quote_at<=now or (now-q.quote_at).total_seconds()>10:raise ValueError('STALE_REAL_QUOTE')
            if not q.quote_at<=self.quote_received_at[iid]<=now or (now-self.quote_received_at[iid]).total_seconds()>10:
                raise ValueError('STALE_REAL_QUOTE_RECEIPT')
            if not 0<q.bid_usd<=q.ask_usd:raise ValueError('INVALID_REAL_QUOTE')
        observed_equity=self.settled+self.unsettled+sum((qty*quotes[iid].bid_usd for iid,qty in self.positions.items()),Decimal(0))
        # Broker displayed equity may use a last trade rather than liquidation bid.
        # Cash and quantities reconcile exactly; the controller uses its own bid equity.
        if not observed_equity.is_finite() or not self.equity.is_finite():raise ValueError('REAL_EQUITY_INVALID')
        return session

    def current_candidates(self,now):
        """Expire optional discovery rows without changing source timestamps or account TTL."""
        from dataclasses import replace
        if not self.context.as_of<=now<self.context.expires_at:raise ValueError('STALE_REAL_CONTEXT')
        kept=tuple(q for q in self.context.instruments if q.instrument_id in self.positions
            or 0<=(now-q.quote_at).total_seconds()<=10)
        if not kept:raise ValueError('NO_FRESH_REAL_PORTFOLIO_CONTEXT')
        ids={q.instrument_id for q in kept};at=self.context.as_of
        receipts={iid:value for iid,value in self.quote_received_at.items() if iid in ids}
        bars=[max(q.bars,key=lambda bar:bar.at).at for q in kept if q.bars]
        proof={**self.market_data_evidence,'nasdaq_symbols':[q.symbol for q in kept],
            'quote_max_age_ms':max((at-q.quote_at).total_seconds()*1000 for q in kept),
            'receipt_max_age_ms':max((at-r).total_seconds()*1000 for r in receipts.values())}
        return replace(self,instruments=tuple(i for i in self.instruments if i.instrument_id in ids),
            context=self.context.model_copy(update={'instruments':kept}),quote_received_at=receipts,market_data_evidence=proof)

    def validate_market_data_authority(self,now):
        proof=self.market_data_evidence
        required={'status','host','session_label','asset_scope','nasdaq_symbols','quote_max_age_ms',
            'receipt_max_age_ms','bar_max_age_seconds','entitlement_evidence_sha256'}
        if not isinstance(proof,dict) or set(proof)!=required:raise ValueError('REAL_MARKET_COVERAGE_UNVERIFIED')
        session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        if session is None or not session.contains(now):raise ValueError('REGULAR_SESSION_REQUIRED')
        if (proof['status']!='VERIFIED_CURRENT_NASDAQ_STOCK_COVERAGE' or proof['host']!='api.webull.com'
                or proof['session_label']!=session.label.isoformat() or proof['asset_scope']!='US_LISTED_STOCK'
                or not isinstance(proof['nasdaq_symbols'],list) or not proof['nasdaq_symbols']
                or not isinstance(proof['entitlement_evidence_sha256'],str)
                or len(proof['entitlement_evidence_sha256'])!=64):
            raise ValueError('REAL_MARKET_COVERAGE_UNVERIFIED')
        if (not 0<=proof['quote_max_age_ms']<=10000 or not Decimal(str(proof['quote_max_age_ms'])).is_finite()
                or not 0<=proof['receipt_max_age_ms']<=10000 or not Decimal(str(proof['receipt_max_age_ms'])).is_finite()
                or proof['bar_max_age_seconds']<0 or not Decimal(str(proof['bar_max_age_seconds'])).is_finite()):
            raise ValueError('REAL_MARKET_COVERAGE_STALE')
        contexts={item.symbol:item for item in self.context.instruments}
        refs={item.ticker:item for item in self.instruments}
        if set(proof['nasdaq_symbols'])!=set(contexts):raise ValueError('REAL_MARKET_COVERAGE_MISMATCH')
        for symbol in proof['nasdaq_symbols']:
            from .real_market import TRADABLE_LISTINGS
            if symbol not in contexts or symbol not in refs or refs[symbol].listing_exchange not in TRADABLE_LISTINGS:
                raise ValueError('REAL_MARKET_COVERAGE_MISMATCH')
        return proof


@dataclass(frozen=True)
class RealExitPacket:
    account_binding: str
    observed_at: datetime
    settled: Decimal
    unsettled: Decimal
    equity: Decimal
    positions: dict
    open_client_ids: tuple
    instrument_id: str
    symbol: str
    bid_usd: Decimal
    ask_usd: Decimal
    quote_at: datetime
    quote_received_at: datetime
    evidence_id: str
    host: str='api.webull.com'
    account_type: str='INDIVIDUAL_CASH'

    def validate(self,now):
        if self.host!='api.webull.com' or self.account_type!='INDIVIDUAL_CASH':raise ValueError('REAL_CASH_ACCOUNT_REQUIRED')
        if not self.account_binding.startswith('real-'):raise ValueError('REAL_ACCOUNT_BINDING_REQUIRED')
        session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        if session is None or not session.contains(now):raise ValueError('REGULAR_SESSION_REQUIRED')
        if (not 0<=(now-self.observed_at).total_seconds()<=10
                or not 0<=(now-self.quote_at).total_seconds()<=10
                or not 0<=(now-self.quote_received_at).total_seconds()<=10):
            raise ValueError('STALE_REAL_EXIT_OBSERVATION')
        if self.settled<0 or self.unsettled<0 or not self.equity.is_finite():raise ValueError('REAL_CASH_INVALID')
        if not 0<self.bid_usd<=self.ask_usd:raise ValueError('INVALID_REAL_QUOTE')
        if self.instrument_id not in self.positions or self.positions[self.instrument_id]<=0:
            raise ValueError('REAL_EXIT_OWNERSHIP_REQUIRED')
        return session


@dataclass(frozen=True)
class RealOwnershipPacket:
    account_binding: str
    observed_at: datetime
    settled: Decimal
    unsettled: Decimal
    equity: Decimal
    positions: dict
    open_client_ids: tuple
    instrument_id: str
    symbol: str
    evidence_id: str
    host: str='api.webull.com'
    account_type: str='INDIVIDUAL_CASH'

    def validate(self,now):
        if self.host!='api.webull.com' or self.account_type!='INDIVIDUAL_CASH':raise ValueError('REAL_CASH_ACCOUNT_REQUIRED')
        if not self.account_binding.startswith('real-'):raise ValueError('REAL_ACCOUNT_BINDING_REQUIRED')
        session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        if session is None or not session.contains(now):raise ValueError('REGULAR_SESSION_REQUIRED')
        if not 0<=(now-self.observed_at).total_seconds()<=10:raise ValueError('STALE_REAL_ACCOUNT')
        if self.settled<0 or self.unsettled<0 or not self.equity.is_finite():raise ValueError('REAL_CASH_INVALID')
        if self.instrument_id not in self.positions or self.positions[self.instrument_id]<=0:
            raise ValueError('REAL_EXIT_OWNERSHIP_REQUIRED')
        return session


@dataclass(frozen=True)
class RealAccountOrderPacket:
    account_binding: str
    observed_at: datetime
    settled: Decimal
    unsettled: Decimal
    equity: Decimal
    positions: dict
    open_client_ids: tuple
    instrument_id: str
    symbol: str
    evidence_id: str
    host: str='api.webull.com'
    account_type: str='INDIVIDUAL_CASH'

    def validate(self,now):
        if self.host!='api.webull.com' or self.account_type!='INDIVIDUAL_CASH':raise ValueError('REAL_CASH_ACCOUNT_REQUIRED')
        if not self.account_binding.startswith('real-'):raise ValueError('REAL_ACCOUNT_BINDING_REQUIRED')
        session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        if session is None or not session.contains(now):raise ValueError('REGULAR_SESSION_REQUIRED')
        if not 0<=(now-self.observed_at).total_seconds()<=10:raise ValueError('STALE_REAL_ACCOUNT')
        if self.settled<0 or self.unsettled<0 or not self.equity.is_finite():raise ValueError('REAL_CASH_INVALID')
        return session


def confirm_flat_settlements(controller,account,emit=None):
    """Broker-truth (owner 2026-10-18): bring the ledger's cash to the broker's figures before any comparison.
    Only from a valid observation (bound, fresh, consistent) of an account quiet on both sides with positions
    identical to the ledger's (broker_truth.eligible); otherwise nothing changes and exact checks apply."""
    from .broker_truth import apply,valid_observation
    from .carry import broker_positions_by_instrument
    cash=account.get('cash') or {}
    if 'settled_cash' not in cash or 'unsettled_cash' not in cash:return controller.state
    try:
        if not valid_observation(account,controller.account_id,controller.clock()):return controller.state
        positions=broker_positions_by_instrument(account,controller.state)
    except Exception:return controller.state
    apply(controller,Decimal(cash['settled_cash']),Decimal(cash['unsettled_cash']),account['evidence_id'],
        broker_positions=positions,broker_open_ids=tuple(account.get('open_client_ids') or ()),emit=emit)
    return controller.state


def flat_recovery_values(account,state,account_id,now):
    """Exact-flat account reconciliation has no quote or order authority."""
    if account.get('account_binding')!=account_id:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    observed,received=account.get('observed_at'),account.get('received_at')
    if (not isinstance(observed,datetime) or not isinstance(received,datetime)
            or observed.tzinfo is None or received.tzinfo is None or not observed<=received<=now
            or not 0<=(now-observed).total_seconds()<=10):raise ValueError('STALE_REAL_ACCOUNT')
    if (state['positions'] or state['reservations'] or account.get('open_client_ids')
            or any(not row['final'] or row.get('phase')=='UNKNOWN' for row in state['orders'].values())):
        raise ValueError('REAL_RECONCILIATION_RECOVERY_NOT_FLAT')
    positions=account.get('positions')
    if not isinstance(positions,(list,tuple)):raise ValueError('REAL_ACCOUNT_SHAPE_OR_PAGINATION')
    if any(Decimal(row['quantity'])!=0 for row in positions):raise ValueError('REAL_RECONCILIATION_RECOVERY_NOT_FLAT')
    cash=account['cash']
    settled,unsettled,equity=(Decimal(cash[key]) for key in ('settled_cash','unsettled_cash','net_liquidation_value'))
    total=Decimal(cash['cash_balance'])
    if (not all(value.is_finite() and value>=0 for value in (settled,unsettled,equity,total))
            or settled+unsettled!=total):raise ValueError('REAL_CASH_INVALID')
    local=state['cash']
    if (settled!=Decimal(local['settled_usd']) or unsettled!=Decimal(local['unsettled_usd'])
            or equity!=Decimal(local['liquidation_equity_usd']) or equity!=total):
        raise ValueError('REAL_RECONCILIATION_RECOVERY_CASH_MISMATCH')
    return settled,unsettled,equity


class RealGate:
    def __init__(self,root,account_binding,observation_db=None):
        self.root=Path(root);self.account=account_binding
        self.db=observation_db or LocalDatabase('postgres','m2_admin',schema='real_readiness')

    def _checkpoint_path(self,expected_sha):
        matches=[p for p in (data_root(self.root)/'.local/real-readiness').glob('flat-checkpoint-*.json')
            if sha256(p.read_bytes()).hexdigest().upper()==str(expected_sha).upper()]
        if len(matches)!=1:raise ValueError('REAL_FLAT_CHECKPOINT_REQUIRED')
        return matches[0]

    @staticmethod
    def _packet_path(root,expected_sha):
        base=data_root(root)/'.local/real-readiness'
        matches=[p for pattern in ('zero-order-packet-*.json','daily-session-*.json') for p in base.glob(pattern)
            if sha256(p.read_bytes()).hexdigest().upper()==str(expected_sha).upper()]
        if len(matches)!=1:raise ValueError('REAL_MARKET_READINESS_REQUIRED')
        return matches[0]

    @classmethod
    def market_authority(cls,root,receipt):
        try:packet=json.loads(cls._packet_path(root,receipt['market_readiness_sha256']).read_text(encoding='utf-8'))
        except (OSError,ValueError):raise ValueError('REAL_MARKET_READINESS_INVALID') from None
        entitlement=packet.get('entitlement_evidence')
        if not isinstance(entitlement,dict):raise ValueError('REAL_ENTITLEMENT_UNVERIFIED')
        return {'session_label':receipt['market_session_label'],
            'entitlement_evidence_sha256':digest(entitlement)}

    def checkpoint(self,expected_sha):
        value,actual=load_checkpoint(self._checkpoint_path(expected_sha),account_binding=self.account,
            expected_sha256=expected_sha)
        with self.db.connect() as c:
            row=c.execute('SELECT version,payload FROM account_state WHERE singleton').fetchone()
        if (not row or row[0]!=value['local_integrity']['observation_ledger_version']
                or row[1].get('account_binding')!=self.account):
            raise ValueError('REAL_FLAT_CHECKPOINT_BINDING')
        return value,actual

    @staticmethod
    def offline_preparation(root,now):
        """One-session read-only evidence collection authority; never order authority."""
        root=Path(root)
        try:value=json.loads((data_root(root)/'.local/real-readiness/judicator-real-prepare.json').read_text())
        except (OSError,ValueError):raise ValueError('JUDICATOR_REAL_PREPARATION_REQUIRED') from None
        required={'verdict','scope','account_binding','build_sha256','flat_checkpoint_sha256',
            'market_session_label','issued_at','expires_at'}
        if set(value)!=required:raise ValueError('JUDICATOR_REAL_PREPARATION_INVALID')
        try:
            issued=datetime.fromisoformat(value['issued_at']);expires=datetime.fromisoformat(value['expires_at'])
            session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
        except (TypeError,ValueError):raise ValueError('JUDICATOR_REAL_PREPARATION_INVALID') from None
        if (value['verdict']!='PREPARE' or value['scope']!='REAL_READ_ONLY_READINESS_V2'
                or not isinstance(value['account_binding'],str) or not value['account_binding'].startswith('real-')
                or value['build_sha256']!=build_digest(root) or issued.tzinfo is None or expires.tzinfo is None
                or session is None or not session.contains(now) or value['market_session_label']!=session.label.isoformat()
                or not session.contains(issued) or not issued<=now<=expires<=session.closes_at
                or expires-issued>timedelta(hours=1)):
            raise ValueError('JUDICATOR_REAL_PREPARATION_INVALID')
        checkpoints=[p for p in (data_root(root)/'.local/real-readiness').glob('flat-checkpoint-*.json')
            if sha256(p.read_bytes()).hexdigest().upper()==str(value['flat_checkpoint_sha256']).upper()]
        if len(checkpoints)!=1:raise ValueError('REAL_FLAT_CHECKPOINT_REQUIRED')
        checkpoint,_=load_checkpoint(checkpoints[0],account_binding=value['account_binding'],
            expected_sha256=value['flat_checkpoint_sha256'])
        return value

    @staticmethod
    def offline_account_preparation(root,now):
        """Exact-build authority for bounded after-hours account reads only."""
        root=Path(root)
        try:value=json.loads((data_root(root)/'.local/real-readiness/judicator-real-account-prepare.json').read_text())
        except (OSError,ValueError):raise ValueError('JUDICATOR_REAL_ACCOUNT_PREPARATION_REQUIRED') from None
        required={'verdict','scope','account_binding','build_sha256','flat_checkpoint_sha256','issued_at','expires_at'}
        try:issued=datetime.fromisoformat(value['issued_at']);expires=datetime.fromisoformat(value['expires_at'])
        except (KeyError,TypeError,ValueError):raise ValueError('JUDICATOR_REAL_ACCOUNT_PREPARATION_INVALID') from None
        if (set(value)!=required or value['verdict']!='PREPARE'
                or value['scope']!='REAL_ACCOUNT_READ_ONLY_AFTER_HOURS_V1'
                or not isinstance(value['account_binding'],str) or not value['account_binding'].startswith('real-')
                or value['build_sha256']!=build_digest(root) or issued.tzinfo is None or expires.tzinfo is None
                or not issued<=now<=expires or expires-issued>timedelta(hours=1)):
            raise ValueError('JUDICATOR_REAL_ACCOUNT_PREPARATION_INVALID')
        checkpoints=[p for p in (data_root(root)/'.local/real-readiness').glob('flat-checkpoint-*.json')
            if sha256(p.read_bytes()).hexdigest().upper()==str(value['flat_checkpoint_sha256']).upper()]
        if len(checkpoints)!=1:raise ValueError('REAL_FLAT_CHECKPOINT_REQUIRED')
        load_checkpoint(checkpoints[0],account_binding=value['account_binding'],
            expected_sha256=value['flat_checkpoint_sha256'])
        return value

    @staticmethod
    def offline_checkpoint_preparation(root,now):
        """Exact-build authority for one bounded V2 checkpoint read and write."""
        root=Path(root)
        try:value=json.loads((data_root(root)/'.local/real-readiness/judicator-real-checkpoint-prepare.json').read_text())
        except (OSError,ValueError):raise ValueError('JUDICATOR_REAL_CHECKPOINT_PREPARATION_REQUIRED') from None
        required={'verdict','scope','account_binding','build_sha256','base_checkpoint_sha256',
            'account_diagnostic_sha256','cash_activity_diagnostic_sha256','issued_at','expires_at'}
        try:issued=datetime.fromisoformat(value['issued_at']);expires=datetime.fromisoformat(value['expires_at'])
        except (KeyError,TypeError,ValueError):raise ValueError('JUDICATOR_REAL_CHECKPOINT_PREPARATION_INVALID') from None
        from .real_flat_checkpoint import (BASE_CHECKPOINT_SHA256,ACCOUNT_DIAGNOSTIC_SHA256,
            CASH_ACTIVITY_DIAGNOSTIC_SHA256)
        if (set(value)!=required or value['verdict']!='PREPARE'
                or value['scope']!='REAL_FLAT_CHECKPOINT_V2_CREATE'
                or not isinstance(value['account_binding'],str) or not value['account_binding'].startswith('real-')
                or value['build_sha256']!=build_digest(root)
                or value['base_checkpoint_sha256']!=BASE_CHECKPOINT_SHA256
                or value['account_diagnostic_sha256']!=ACCOUNT_DIAGNOSTIC_SHA256
                or value['cash_activity_diagnostic_sha256']!=CASH_ACTIVITY_DIAGNOSTIC_SHA256
                or issued.tzinfo is None or expires.tzinfo is None or not issued<=now<=expires
                or expires-issued>timedelta(hours=1)):
            raise ValueError('JUDICATOR_REAL_CHECKPOINT_PREPARATION_INVALID')
        return value

    @staticmethod
    def offline_receipt(root,now,*,recovery=False):
        """Validate immutable public authority evidence before any DB/secret/network object exists."""
        root=Path(root)
        from .real_daily import daily_receipt
        # Owner authority comes only from the protected signed record. Neither
        # the presence nor absence of a workspace JSON file can select a fallback.
        return daily_receipt(root,now,recovery=recovery)

    def approved(self,receipt=None,now=None):
        # Judicator supplies this artifact after reviewing this exact build.
        # Owner activation authorization already exists; this is technical GO only.
        value=receipt or self.offline_receipt(self.root,now or datetime.now(timezone.utc))
        if value.get('service_only') or value.get('verdict')!='GO':raise ValueError('REAL_ENTRY_AUTHORITY_REQUIRED')
        if value['account_binding']!=self.account:
            raise ValueError('JUDICATOR_REAL_BUILD_OR_SCOPE_MISMATCH')
        checkpoint,_=self.checkpoint(value['flat_checkpoint_sha256'])
        return checkpoint

    def readiness(self,packet,now,*,initial=False,checkpoint_sha256):
        checkpoint,_=self.checkpoint(checkpoint_sha256)
        if packet.account_binding!=self.account:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        packet.validate(now,flat=initial)
        if initial:self._cash_continuity(packet,checkpoint)
        return checkpoint

    def read_checked(self,fetch,clock,*,recovery=False,flat_state=None,initial=False):
        """Audit authority before acquiring perishable evidence; never cache it.

        The acquisition callback is invoked only after the full installed-build
        and checkpoint checks. Every observation is then checked at current time.
        No caller-supplied receipt can skip the audit.
        """
        from .real_owner_trust import authority_revision
        revision=authority_revision(self.root)
        receipt=self.offline_receipt(self.root,clock(),recovery=recovery)
        completed=self.approved(receipt,clock())
        authority=None if flat_state is not None else self.market_authority(self.root,receipt)
        deadline=receipt['recovery_not_after'] if recovery else receipt['expires_at']
        if not datetime.fromisoformat(receipt['issued_at'])<=clock()<datetime.fromisoformat(deadline):
            raise ValueError('REAL_DAILY_AUTHORITY_EXPIRED')
        observation=fetch()
        now=clock()
        if authority_revision(self.root)!=revision:raise ValueError('REAL_OWNER_AUTHORITY_CHANGED')
        if not datetime.fromisoformat(receipt['issued_at'])<=now<datetime.fromisoformat(deadline):
            raise ValueError('REAL_DAILY_AUTHORITY_EXPIRED')
        if flat_state is not None:
            flat_recovery_values(observation,flat_state,self.account,now)
        else:
            if hasattr(observation,'current_candidates'):observation=observation.current_candidates(now)
            if observation.account_binding!=self.account:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
            observation.validate(now,flat=initial)
            proof=observation.validate_market_data_authority(now)
            if (proof['entitlement_evidence_sha256']!=authority['entitlement_evidence_sha256']
                    or (not recovery and proof['session_label']!=authority['session_label'])):
                raise ValueError('REAL_ENTITLEMENT_UNVERIFIED')
            if initial:self._cash_continuity(observation,completed)
        return observation,completed

    def check(self,packet,now,*,initial=False):
        receipt=self.offline_receipt(self.root,now);completed=self.approved(receipt,now)
        if packet.account_binding!=self.account:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        packet.validate(now,flat=initial);proof=packet.validate_market_data_authority(now)
        authority=self.market_authority(self.root,receipt)
        if (proof['session_label']!=authority['session_label']
                or proof['entitlement_evidence_sha256']!=authority['entitlement_evidence_sha256']):
            raise ValueError('REAL_ENTITLEMENT_UNVERIFIED')
        if initial:self._cash_continuity(packet,completed)
        return completed

    def _protective_receipt(self,now):
        """Authority for risk-reducing actions on owned shares (exit observation, exit, cancel before exit).
        Normally the session certificate (recovery scope). With carried positions and no current certificate
        (e.g. preparation failed today, or a release changed the build), exits must still be possible: the
        owner-signed charter (signature, expiry, revocation, running build) plus the regular session then
        authorize EXITS ONLY. Entry paths never use this receipt."""
        try:return self.offline_receipt(self.root,now,recovery=True)
        except ValueError:
            from .real_daily import load_authority,digest
            authority=load_authority(self.root,now)
            session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
            if not session or not session.contains(now):raise
            return dict(verdict='GO',scope='REAL_PROTECTIVE_EXIT_V1',account_binding=authority['account_binding'],
                build_sha256=authority['build_sha256'],flat_checkpoint_sha256=authority['flat_checkpoint_sha256'],
                market_session_label=session.label.isoformat(),issued_at=session.opens_at.isoformat(),
                expires_at=session.closes_at.isoformat(),daily_authority_sha256=digest(authority))

    def check_flat_recovery(self,account,now,state):
        receipt=self.offline_receipt(self.root,now,recovery=True)
        completed=self.approved(receipt,now)
        flat_recovery_values(account,state,self.account,now)
        return completed

    def check_recovery(self,packet,now):
        receipt=self.offline_receipt(self.root,now,recovery=True);completed=self.approved(receipt,now)
        if packet.account_binding!=self.account:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        packet.validate(now);proof=packet.validate_market_data_authority(now)
        authority=self.market_authority(self.root,receipt)
        if proof['entitlement_evidence_sha256']!=authority['entitlement_evidence_sha256']:
            raise ValueError('REAL_ENTITLEMENT_UNVERIFIED')
        return completed

    def check_exit(self,packet,now,intent,state):
        completed=self.check_exit_observation(packet,now,state)
        if (packet.account_binding!=self.account or intent.account_id!=self.account
                or intent.environment!='REAL' or intent.side!='SELL_OWNED'
                or intent.instrument_id!=packet.instrument_id
                or state['instruments'][intent.instrument_id]['ticker']!=packet.symbol):
            raise ValueError('REAL_EXIT_AUTHORITY_REQUIRED')
        durable={iid:Decimal(row['quantity']) for iid,row in state['positions'].items()}
        reserved=sum((Decimal(row['shares']) for key,row in state['reservations'].items()
            if key!=intent.intent_id and row['instrument_id']==intent.instrument_id),Decimal(0))
        if intent.quantity<=0 or intent.quantity>durable[intent.instrument_id]-reserved:
            raise ValueError('REAL_EXIT_QUANTITY_AUTHORITY')
        return completed

    def check_exit_observation(self,packet,now,state):
        """Authorize monitoring from the narrow held-instrument path only."""
        receipt=self._protective_receipt(now);completed=self.approved(receipt,now)
        packet.validate(now)
        if packet.account_binding!=self.account:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        durable={iid:Decimal(row['quantity']) for iid,row in state['positions'].items()}
        if packet.positions!=durable:raise ValueError('REAL_EXIT_POSITION_MISMATCH')
        known={row['intent']['client_order_id'] for row in state['orders'].values() if not row['final']}
        if set(packet.open_client_ids)-known:raise ValueError('UNKNOWN_REAL_ORDER')
        return completed

    def check_exit_ownership(self,packet,now,state):
        return self.check_exit_observation(packet,now,state)

    def check_buy_cancel(self,packet,now,state):
        receipt=self._protective_receipt(now);completed=self.approved(receipt,now)
        packet.validate(now)
        if (packet.account_binding!=self.account
                or state['instruments'][packet.instrument_id]['ticker']!=packet.symbol):
            raise ValueError('REAL_EXIT_CANCEL_AUTHORITY_REQUIRED')
        durable={iid:Decimal(row['quantity']) for iid,row in state['positions'].items()}
        if packet.positions!=durable:raise ValueError('REAL_EXIT_POSITION_MISMATCH')
        known={row['intent']['client_order_id'] for row in state['orders'].values() if not row['final']}
        if set(packet.open_client_ids)-known:raise ValueError('UNKNOWN_REAL_ORDER')
        return completed

    @staticmethod
    def _cash_continuity(packet,checkpoint):
        cash=checkpoint['cash'];baseline=Decimal(cash['settled_usd'])+Decimal(cash['unsettled_usd'])
        if packet.settled+packet.unsettled!=baseline or packet.equity!=Decimal(cash['equity_usd']):
            raise ValueError('REAL_FLAT_CHECKPOINT_CASH_CHANGED')


def open_session_if_due(controller,state):
    """Open today's session in the ledger from an exact flat/carry reconciliation (no quotes needed), so a
    prior day's daily-loss latch resets even when market data is not ready at the open."""
    try:
        label=state['session']['label'];ready=state['reconciliation']['ready']
        # With positions the session opens only from a quoted refresh, whose opening marks fix the day's
        # starting equity after any overnight gap (NewSession.opening_marks).
        if state['positions']:return state
    except (KeyError,TypeError):return state
    now=controller.clock();session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
    if session and session.contains(now) and label<session.label.isoformat() and ready:
        state=controller.send(NewSession(session=session))
    return state


class RealController(Controller):
    def __init__(self,ledger,account_id,clock):
        if ledger.execution_scope!='REAL':raise ValueError('REAL_LEDGER_REQUIRED')
        super().__init__(ledger,account_id,clock)

    def admit(self,intent,plan,*,policy_hash=None):
        if getattr(self,'waiting_for_market_data',False) and intent.side=='BUY':raise ValueError('REAL_MARKET_DATA_NOT_READY')
        if getattr(self,'recovery_only',False) and intent.side=='BUY':raise ValueError('REAL_RECOVERY_REDUCE_ONLY')
        if intent.side=='BUY':
            packet=getattr(self,'current_market_packet',None)
            if packet is not None:
                now=self.clock();session=packet.validate(now,entry=True)
                q=next((q for q in packet.context.instruments if q.instrument_id==intent.instrument_id),None)
                if q is None:raise ValueError('REAL_ENTRY_QUOTE_MISSING')
                self.send(Mark(instrument_id=q.instrument_id,bid_usd=q.bid_usd,observed_at=q.quote_at,source_id=SOURCE,
                    received_at=packet.quote_received_at[q.instrument_id],
                    valid_until=min(packet.context.expires_at,session.closes_at,q.quote_at+timedelta(seconds=10))))
        fee=Decimal('1')+intent.quantity*Decimal('0.02')
        stress=intent.quantity*intent.maximum_entry_usd*Decimal('0.001') if intent.side=='BUY' else Decimal(0)
        state=self.send(Admit(intent=intent,fee_allowance_usd=fee,exit_cost_usd=fee,stress_allowance_usd=stress,
            latest_exit_at=plan.latest_exit_at,max_holding_seconds=max(1,int((plan.latest_exit_at-self.clock()).total_seconds()))))
        return state['outbox'][intent.intent_id]['nonce']

    def downgrade_to_recovery(self,reason):
        if not getattr(self,'recovery_only',False):
            if not any(row.get('reason')==reason for row in self.state.get('incidents',{}).values()):
                self.incident(reason)
            self.recovery_only=True
        return self.state

    def refresh(self,packet):
        now=self.clock();session=packet.validate(now)
        if packet.account_binding!=self.account_id:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        self.lease()
        self._confirm_due_settlements(packet,now)
        additions=tuple(i for i in packet.instruments if i.instrument_id not in self.state['instruments'])
        if additions:self.send(RegisterInstruments(instruments=additions,evidence_id=packet.evidence_id))
        def mark(q):
            return Mark(instrument_id=q.instrument_id,bid_usd=q.bid_usd,observed_at=q.quote_at,source_id=SOURCE,
                received_at=packet.quote_received_at[q.instrument_id],
                valid_until=min(packet.context.expires_at,session.closes_at,q.quote_at+timedelta(seconds=10)))
        def marks():
            required_marks=set(self.state['positions'])|{o['intent']['instrument_id']
                for o in self.state['orders'].values() if not o['final']}
            for q in packet.context.instruments:
                if q.instrument_id not in required_marks:continue
                prior=self.state['marks'].get(q.instrument_id)
                if prior and datetime.fromisoformat(prior['observed_at'])>=q.quote_at:continue
                self.send(mark(q))
        def reconcile():
            clients={o['intent']['client_order_id']:key for key,o in self.state['orders'].items() if not o['final']}
            if set(packet.open_client_ids)-set(clients):raise ValueError('UNKNOWN_REAL_ORDER')
            self.send(Reconcile(settled_usd=packet.settled,unsettled_usd=packet.unsettled,
                positions=tuple(packet.positions.items()),outstanding_intent_ids=tuple(clients[c] for c in packet.open_client_ids),
                evidence_id=packet.evidence_id,settlement_known=True))
        if self.state['session']['label']<session.label.isoformat():
            # First refresh of a session: an exact account reconciliation opens the new session, then
            # carried positions receive marks valid for that session (marks are session-bound).
            reconcile()
            if self.state['reconciliation']['ready']:
                held=set(self.state['positions'])
                fresh=self.clock()
                # Only comfortably fresh quotes: a mark failing its 10 s validity would reject the whole NEW_SESSION.
                self.send(NewSession(session=session,opening_marks=tuple(mark(q) for q in packet.context.instruments
                    if q.instrument_id in held and session.opens_at<=q.quote_at
                    and (fresh-q.quote_at).total_seconds()<=5)))
            marks()
        else:
            marks();reconcile()
        if not self.state['reconciliation']['ready']:raise ValueError('REAL_RECONCILIATION_FAILED')
        self.current_market_packet=packet
        return self.state

    def _confirm_due_settlements(self,packet,now):
        from .broker_truth import apply,eligible
        if eligible(self.state,packet.positions,packet.open_client_ids):
            apply(self,packet.settled,packet.unsettled,packet.evidence_id,
                broker_positions=packet.positions,broker_open_ids=packet.open_client_ids)
            return
        state=self.state
        delta=packet.settled-Decimal(state['cash']['settled_usd'])
        if delta>0:
            lots=[(k,v) for k,v in state['settlements'].items() if Decimal(v['outstanding_usd'])>0]
            due=settlement_observation_date(state,now)
            total=sum((Decimal(v['outstanding_usd']) for k,v in lots),Decimal(0))
            if (delta!=total or packet.unsettled!=Decimal(state['cash']['unsettled_usd'])-total
                    or any(date.fromisoformat(v['settlement_date'])>due for k,v in lots)):
                raise ValueError('SETTLEMENT_CHANGE_UNRECONCILED')
            for key,lot in lots:
                self.send(ConfirmSettlement(execution_id=key,amount_usd=lot['amount_usd'],
                    effective_on=date.fromisoformat(lot['settlement_date']),evidence_id=packet.evidence_id))

    def open_session_from_exit_packets(self,packets,session):
        """Protection path (carried positions, no quoted entry refresh possible): open today's session from
        exact held-instrument packets -- settlement, exact Reconcile, then NEW_SESSION with opening marks."""
        now=self.clock();first=packets[0]
        if first.account_binding!=self.account_id:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        self.lease()
        self._confirm_due_settlements(first,now)
        clients={o['intent']['client_order_id']:key for key,o in self.state['orders'].items() if not o['final']}
        if set(first.open_client_ids)-set(clients):raise ValueError('UNKNOWN_REAL_ORDER')
        self.send(Reconcile(settled_usd=first.settled,unsettled_usd=first.unsettled,
            positions=tuple(first.positions.items()),outstanding_intent_ids=tuple(clients[c] for c in first.open_client_ids),
            evidence_id=first.evidence_id,settlement_known=True))
        if not self.state['reconciliation']['ready']:raise ValueError('REAL_RECONCILIATION_FAILED')
        marks=tuple(Mark(instrument_id=p.instrument_id,bid_usd=p.bid_usd,observed_at=p.quote_at,source_id=SOURCE,
                received_at=p.quote_received_at,valid_until=min(session.closes_at,p.quote_at+timedelta(seconds=10)))
            for p in packets if session.opens_at<=p.quote_at and (now-p.quote_at).total_seconds()<=5)
        return self.send(NewSession(session=session,opening_marks=marks))

    def refresh_for_exit(self,packet):
        """Re-establish current account evidence from a validated exit/ownership packet.

        Exits must never depend on a refresh that happened earlier in the cycle: reconciliation
        evidence lapses after reconciliation_max_age_seconds and a stale ledger would reject the
        very SELL that protects the position. A real mismatch still opens an incident."""
        now=self.clock()
        if packet.account_binding!=self.account_id:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        self.lease();state=self.state
        if getattr(packet,'bid_usd',None) is not None and packet.instrument_id in state['positions']:
            session=reviewed_calendar().session(now.astimezone(NEW_YORK).date())
            prior=state['marks'].get(packet.instrument_id)
            if not (prior and datetime.fromisoformat(prior['observed_at'])>packet.quote_at):
                self.send(Mark(instrument_id=packet.instrument_id,bid_usd=packet.bid_usd,observed_at=packet.quote_at,
                    source_id=SOURCE,received_at=packet.quote_received_at,
                    valid_until=min(session.closes_at,packet.quote_at+timedelta(seconds=10))))
        clients={o['intent']['client_order_id']:key for key,o in self.state['orders'].items() if not o['final']}
        if set(packet.open_client_ids)-set(clients):raise ValueError('UNKNOWN_REAL_ORDER')
        self.send(Reconcile(settled_usd=packet.settled,unsettled_usd=packet.unsettled,
            positions=tuple(packet.positions.items()),outstanding_intent_ids=tuple(clients[c] for c in packet.open_client_ids),
            evidence_id=packet.evidence_id,settlement_known=True))
        if not self.state['reconciliation']['ready']:raise ValueError('REAL_RECONCILIATION_FAILED')
        return self.state

    def refresh_flat_recovery(self,account):
        """Append normal reconciliation, preserving every existing incident/latch."""
        confirm_flat_settlements(self,account)
        settled,unsettled,_=flat_recovery_values(account,self.state,self.account_id,self.clock())
        state=self.send(Reconcile(settled_usd=settled,unsettled_usd=unsettled,positions=(),
            outstanding_intent_ids=(),evidence_id=account['evidence_id'],settlement_known=True))
        return open_session_if_due(self,state)

    def refresh_carry_recovery(self,account):
        """Exact reconciliation of an account carrying charter-authorized positions outside the session
        (no quotes): cash buckets and every quantity must equal the ledger; incidents/latches are kept."""
        from .carry import carry_account_values
        self.lease()
        confirm_flat_settlements(self,account)
        settled,unsettled,positions=carry_account_values(account,self.state,self.account_id,self.clock())
        state=self.send(Reconcile(settled_usd=settled,unsettled_usd=unsettled,positions=tuple(positions.items()),
            outstanding_intent_ids=(),evidence_id=account['evidence_id'],settlement_known=True))
        return open_session_if_due(self,state)

    def resolve_held_reconciliation_incidents(self,packet,incident_ids):
        """Exact reconciliation while holding that resolves only named operational read failures.

        Broker positions must equal the ledger's exactly, nothing open/unknown on either side, cash equal.
        Positions and cash are never adopted here; a disagreement stays an incident."""
        now=self.clock();packet.validate(now)
        if packet.account_binding!=self.account_id:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
        incident_ids=tuple(incident_ids)
        if not incident_ids:raise ValueError('REAL_RECONCILIATION_INCIDENT_REQUIRED')
        self.lease();state=self.state;incidents=state.get('incidents',{})
        if (set(incident_ids)!=set(incidents) or len(incident_ids)!=len(set(incident_ids))
                or any(incidents[key].get('reason') not in HELD_RECOVERABLE_REASONS
                    or incidents[key].get('pending_correction') is not False for key in incident_ids)):
            raise ValueError('REAL_RECONCILIATION_INCIDENT_SCOPE_INVALID')
        ledger={iid:Decimal(str(row['quantity'])) for iid,row in state['positions'].items() if Decimal(str(row['quantity']))!=0}
        broker={str(k):Decimal(str(v)) for k,v in dict(packet.positions).items() if Decimal(str(v))!=0}
        if (not ledger or broker!=ledger or state['reservations'] or packet.open_client_ids
                or any(not row['final'] or row.get('phase')=='UNKNOWN' for row in state['orders'].values())):
            raise ValueError('REAL_RECONCILIATION_RECOVERY_HELD_MISMATCH')
        if (any(state['latches'].get(name,False) for name in ('daily','drawdown','terminated'))
                or not state['latches'].get('incident',False)
                or not state['reconciliation']['ready'] or not state['reconciliation']['settlement_known']):
            raise ValueError('REAL_RECONCILIATION_RECOVERY_SAFETY_STATE')
        cash=state['cash']
        if packet.settled!=Decimal(cash['settled_usd']) or packet.unsettled!=Decimal(cash['unsettled_usd']):
            raise ValueError('REAL_RECONCILIATION_RECOVERY_CASH_MISMATCH')
        result=self.send(Reconcile(settled_usd=packet.settled,unsettled_usd=packet.unsettled,
            positions=tuple(packet.positions.items()),outstanding_intent_ids=(),evidence_id=packet.evidence_id,
            settlement_known=True,resolved_incident_ids=incident_ids))
        if result.get('incidents') or result['latches'].get('incident'):
            raise ValueError('REAL_RECONCILIATION_INCIDENT_NOT_RESOLVED')
        return result

    def resolve_flat_reconciliation_incidents(self,packet,incident_ids,*,account=None):
        """Append an exact-flat reconciliation that resolves only named incidents."""
        now=self.clock()
        if account is None:
            packet.validate(now,flat=True)
            if packet.account_binding!=self.account_id:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
            settled,unsettled,equity=packet.settled,packet.unsettled,packet.equity
            positions,clients,evidence_id=packet.positions,packet.open_client_ids,packet.evidence_id
        else:
            if packet is not None:raise ValueError('REAL_ACCOUNT_RECOVERY_EVIDENCE_AMBIGUOUS')
            settled,unsettled,equity=flat_recovery_values(account,self.state,self.account_id,now)
            positions=tuple(row for row in account['positions'] if Decimal(row['quantity'])!=0)
            clients,evidence_id=account['open_client_ids'],account['evidence_id']
        incident_ids=tuple(incident_ids)
        if not incident_ids:raise ValueError('REAL_RECONCILIATION_INCIDENT_REQUIRED')
        self.lease();state=self.state;incidents=state.get('incidents',{})
        if (set(incident_ids)!=set(incidents) or len(incident_ids)!=len(set(incident_ids))
                or any(incidents[key].get('reason') not in FLAT_RECOVERABLE_REASONS
                    or incidents[key].get('pending_correction') is not False for key in incident_ids)):
            raise ValueError('REAL_RECONCILIATION_INCIDENT_SCOPE_INVALID')
        if (state['positions'] or state['reservations'] or positions or clients
                or any(not row['final'] or row.get('phase')=='UNKNOWN' for row in state['orders'].values())):
            raise ValueError('REAL_RECONCILIATION_RECOVERY_NOT_FLAT')
        if (any(state['latches'].get(name,False) for name in ('daily','drawdown','terminated'))
                or not state['latches'].get('incident',False)
                or not state['reconciliation']['ready'] or not state['reconciliation']['settlement_known']):
            raise ValueError('REAL_RECONCILIATION_RECOVERY_SAFETY_STATE')
        cash=state['cash']
        if (settled!=Decimal(cash['settled_usd'])
                or unsettled!=Decimal(cash['unsettled_usd'])
                or equity!=Decimal(cash['liquidation_equity_usd'])):
            raise ValueError('REAL_RECONCILIATION_RECOVERY_CASH_MISMATCH')
        result=self.send(Reconcile(settled_usd=settled,unsettled_usd=unsettled,
            positions=(),outstanding_intent_ids=(),evidence_id=evidence_id,
            settlement_known=True,resolved_incident_ids=incident_ids))
        if result.get('incidents') or result['latches'].get('incident'):
            raise ValueError('REAL_RECONCILIATION_INCIDENT_NOT_RESOLVED')
        return result


def open_ledger_from_checkpoint(ledger,checkpoint,packet,clock):
    """First opening of this copy's REAL ledger from a fresh V3 bootstrap checkpoint.

    Used by the daily preparation before any certificate exists, so it relies on
    the checkpoint (already verified against the observation journal by the
    caller) instead of the gate. V3 requires zero unsettled cash, so no
    receivable provenance is needed. Current broker cash must equal the
    checkpoint exactly; otherwise a new bootstrap is required.
    """
    if checkpoint.get('schema')!='REAL_FLAT_CHECKPOINT_V3':raise ValueError('REAL_LEDGER_OPENING_CHECKPOINT_V3_REQUIRED')
    if packet.account_binding!=checkpoint['account_binding']:raise ValueError('REAL_ACCOUNT_BINDING_MISMATCH')
    session=packet.validate(clock(),flat=True)
    RealGate._cash_continuity(packet,checkpoint)
    calendar=reviewed_calendar()
    days=tuple(session.label+timedelta(days=i) for i in range(30)
        if calendar.settlement_day(session.label+timedelta(days=i)))
    ledger.create(CreateAccount(account_id=packet.account_binding,experiment_id='real-cash-500-v1',owner_id='owner',risk_policy=__import__('autonomous_trader.real_configuration',fromlist=['current_limits']).current_limits(),
        initial_settled_usd=Decimal(checkpoint['cash']['settled_usd']),session=session,instruments=packet.instruments,
        capabilities=CapabilityManifest(version=CAPABILITY_VERSION,environment='REAL',entry_order_types=('LIMIT',),
            protection_mechanisms=('LOCAL_MONITORED_STOP',)),settlement_days=days,
        mark_max_age_seconds=2,reconciliation_max_age_seconds=10,opening_receivables=()),
        event_id='real-opening-'+packet.account_binding,at=clock())


def open_real_ledger(ledger,gate,packet,clock):
    """One flat opening after liquidation; external proceeds retain T+1 provenance."""
    try:ledger.snapshot(packet.account_binding)
    except Rejected as exc:
        if str(exc)!='UNKNOWN_ACCOUNT':raise
        completed=gate.check(packet,clock(),initial=True)
        calendar=reviewed_calendar();session=packet.validate(clock(),flat=True)
        days=tuple(session.label+timedelta(days=i) for i in range(30)
            if calendar.settlement_day(session.label+timedelta(days=i)))
        cash=completed['cash'];opening_unsettled=Decimal(cash['unsettled_usd']);receivables=()
        if opening_unsettled:
            traded=max(datetime.fromisoformat(x['filled_at']) for x in completed['liquidation']['fills']).astimezone(NEW_YORK).date()
            due=next((d for d in days if d>traded),None)
            if due is None:raise ValueError('OPENING_SETTLEMENT_DATE_UNAVAILABLE')
            receivables=(OpeningReceivable(evidence_id='owner-liquidation',amount_usd=opening_unsettled,settlement_date=due),)
        ledger.create(CreateAccount(account_id=packet.account_binding,experiment_id='real-cash-500-v1',owner_id='owner',risk_policy=__import__('autonomous_trader.real_configuration',fromlist=['current_limits']).current_limits(),
            initial_settled_usd=Decimal(cash['settled_usd']),session=session,instruments=packet.instruments,
            capabilities=CapabilityManifest(version=CAPABILITY_VERSION,environment='REAL',entry_order_types=('LIMIT',),
                protection_mechanisms=('LOCAL_MONITORED_STOP',)),settlement_days=days,
            mark_max_age_seconds=2,reconciliation_max_age_seconds=10,opening_receivables=receivables),
            event_id='real-opening-'+packet.account_binding,at=clock())
    else:gate.check(packet,clock())
    controller=RealController(ledger,packet.account_binding,clock)
    controller.refresh(packet)
    return controller
