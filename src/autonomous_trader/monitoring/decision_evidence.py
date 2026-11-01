"""Bounded append-only evidence for paid portfolio entry decisions."""
from hashlib import sha256
import json

from ..contracts import (AlphaContext,AlphaDecision,AlphaDecisionEnvelope,
    PortfolioCandidateEvidence,PortfolioDecisionEvidence,payload_hash)
from ..decision_api.astra_probe import (FAST_ENTRY_PROMPT,FAST_ENTRY_VALIDITY,
    PORTFOLIO_ENTRY_PROMPT_VERSION,entry_alpha_schema)


def _hash(value):
    if not isinstance(value,str):value=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return sha256(value.encode()).hexdigest()


PROMPT_SHA256=_hash(FAST_ENTRY_PROMPT+FAST_ENTRY_VALIDITY)
SCHEMA_SHA256=_hash(entry_alpha_schema())


def _prompt(model):
    """(version, sha256) of the entry instructions actually sent to this model."""
    if isinstance(model,str) and model.startswith('claude-'):
        from ..decision_api.claude_prompts import ENTRY_SYSTEM,PROMPT_VERSION
        return PROMPT_VERSION,_hash(ENTRY_SYSTEM)
    return PORTFOLIO_ENTRY_PROMPT_VERSION,PROMPT_SHA256


def _candidate(item):
    unavailable=[]
    for field in ('reference_price_usd','spread_bps','bid_size','ask_size','relative_volume_10d',
            'volume','volatility','price_movement','market_context','sector_context','daily_research'):
        if getattr(item,field) is None:unavailable.append(field)
    daily=item.daily_research
    if daily is not None:
        unavailable.extend('daily_research.'+metric.metric for metric in daily.metrics if metric.value is None)
    source='UNAVAILABLE'
    if item.market_context:
        try:
            value=json.loads(item.market_context)
            if isinstance(value,dict) and isinstance(value.get('market_provenance'),str) and value['market_provenance']:
                source=value['market_provenance']
        except (TypeError,json.JSONDecodeError):pass
    return PortfolioCandidateEvidence(instrument_id=item.instrument_id,symbol=item.symbol,quote_at=item.quote_at,
        bid_usd=item.bid_usd,ask_usd=item.ask_usd,quote_source_id=source,
        candidate_evidence_sha256=_hash(item.model_dump(mode='json')),
        market_context_sha256=_hash(item.market_context) if item.market_context else None,
        daily_bar_payload_sha256=daily.source_payload_sha256 if daily else None,
        unavailable_fields=tuple(sorted(unavailable)))


def build_portfolio_decision_evidence(context:AlphaContext,decision:AlphaDecision,
        envelope:AlphaDecisionEnvelope,ai,*,regime_label='UNCLASSIFIED'):
    """Create a snapshot using only model input and its immediate response."""
    if decision.action not in ('ABSTAIN','OPEN'):raise ValueError('PORTFOLIO_DECISION_ACTION_INVALID')
    if envelope.context_hash!=payload_hash(context) or envelope.decision!=decision:
        raise ValueError('PORTFOLIO_DECISION_BINDING_MISMATCH')
    candidates=tuple(_candidate(item) for item in context.instruments)
    if any(item.quote_at>context.as_of for item in candidates):raise ValueError('PORTFOLIO_FUTURE_EVIDENCE')
    work=ai.get('model_work_id')
    if not isinstance(work,str) or not work:raise ValueError('PORTFOLIO_MODEL_WORK_ID_REQUIRED')
    cost=ai.get('estimated_cost_usd');opened=decision.open if decision.action=='OPEN' else None
    return PortfolioDecisionEvidence(decision_id=envelope.decision_id,action=decision.action,
        decision_snapshot_at=context.as_of,decision_cutoff_at=context.expires_at,
        model_response_at=envelope.created_at,response_within_cutoff=envelope.created_at<=context.expires_at,
        context_sha256=envelope.context_hash,
        candidate_set_sha256=_hash([item.model_dump(mode='json') for item in candidates]),candidates=candidates,
        prompt_version=_prompt(envelope.model)[0],prompt_sha256=_prompt(envelope.model)[1],
        model=envelope.model,reasoning=envelope.reasoning,decision_schema_version=decision.schema_version,
        decision_schema_sha256=SCHEMA_SHA256,model_work_claim_id=work,model_work_result_id=work,
        latency_ms=ai['latency_ms'],model_cost_usd=cost,
        model_cost_status='UNKNOWN' if cost is None else 'FINAL',stated_reason=decision.reason,
        stated_thesis=opened.thesis if opened else None,stated_prediction=decision.reason,
        regime_label=regime_label or 'UNCLASSIFIED',
        stated_horizon_start=context.as_of if opened else None,
        stated_horizon_end=opened.latest_exit_at if opened else None,
        selected_instrument_id=opened.instrument_id if opened else None,selected_symbol=opened.symbol if opened else None,
        entry_exit_plan=opened,order_intent_plan_id=envelope.decision_id if opened else None,
        outcome_plan_id=envelope.decision_id if opened else None,
        episode_id='alpha-'+opened.instrument_id if opened else None)


def persist_portfolio_decision_evidence(store,record,*,mode,at):
    payload=record.model_dump(mode='json');event_id='portfolio-decision-evidence-'+record.decision_id
    method=getattr(store,'emit_idempotent',store.emit)
    method('PORTFOLIO_DECISION_EVIDENCE',payload,mode=mode,at=at,event_id=event_id)
    return event_id
