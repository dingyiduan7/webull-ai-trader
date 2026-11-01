"""Strict local JSON Schema parsing followed by independent semantic checks.

Success means a valid proposal, never an execution authorization.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
import json

from jsonschema import Draft202012Validator, FormatChecker
from .contracts import (AlphaContext, AlphaDecision, DecisionContext, Record,
                        TradeDecisionV1, Trailing, ConditionGroup, ID, UTC,
                        payload_hash, utc)
from .numeric import add, aligned, decimal, floor_quantity, positive


class DecisionError(ValueError):
    pass


class SemanticRegistry(Record):
    strategy_bindings: tuple[tuple[str, str, str], ...]
    policy_hashes: tuple[ID, ...]
    source_ids: tuple[ID, ...]
    execution_policy_ids: tuple[ID, ...]
    partial_fill_policy_ids: tuple[ID, ...]
    trailing_rules: tuple[tuple[str, Literal["NONE", "AMOUNT", "PERCENT", "FEATURE_RULE"]], ...]
    calibration_ids: tuple[ID, ...]
    # Explicit offline test setting, not an approved production default.
    maximum_market_age_ms: int
    maximum_account_age_ms: int


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DecisionError("duplicate JSON key: " + key)
        result[key] = value
    return result


def parse_trade_decision(raw: str | bytes) -> TradeDecisionV1:
    if len(raw) > 131072:
        raise DecisionError("proposal exceeds offline 128 KiB bound")
    try:
        def invalid_constant(value):
            raise DecisionError("nonfinite JSON constant: " + value)
        data = json.loads(raw, parse_float=Decimal, parse_constant=invalid_constant,
                          object_pairs_hook=_no_duplicate_keys)
        schema = json.loads((Path(__file__).parent / "trade-decision.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)
        return TradeDecisionV1.model_validate(data)
    except (ValueError, TypeError, RecursionError) as exc:
        raise DecisionError(str(exc)) from exc
    except Exception as exc:
        from jsonschema.exceptions import ValidationError
        if isinstance(exc, ValidationError):
            raise DecisionError(exc.message) from exc
        raise


def _parse_json_contract(raw: str | bytes, schema_name: str, contract):
    if len(raw) > 32768:
        raise DecisionError("alpha proposal exceeds 32 KiB bound")
    try:
        def invalid_constant(value):
            raise DecisionError("nonfinite JSON constant: " + value)
        data = json.loads(raw, parse_float=Decimal, parse_constant=invalid_constant,
                          object_pairs_hook=_no_duplicate_keys)
        if schema_name == 'alpha-decision.schema.json' and isinstance(data,dict):
            # Only descriptive text is compacted. Identity, prices, quantities,
            # deadlines and JSON types retain strict schema validation.
            if isinstance(data.get('reason'),str):data['reason']=data['reason'][:1000]
            opening=data.get('open')
            if isinstance(opening,dict):
                for key,bound in (('strategy_label',120),('thesis',1000)):
                    if isinstance(opening.get(key),str):opening[key]=opening[key][:bound]
        schema = json.loads((Path(__file__).parent / schema_name).read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)
        return contract.model_validate(data)
    except (ValueError, TypeError, RecursionError) as exc:
        raise DecisionError(str(exc)) from exc
    except Exception as exc:
        from jsonschema.exceptions import ValidationError
        if isinstance(exc, ValidationError):
            raise DecisionError(exc.message) from exc
        raise


def parse_alpha_decision(raw: str | bytes) -> AlphaDecision:
    """Parse the compact model-owned intent. This never grants order authority."""
    return _parse_json_contract(raw, "alpha-decision.schema.json", AlphaDecision)


def validate_entry_window(d: AlphaDecision, c: AlphaContext, now: datetime):
    """Operational validity only; never extend an Astra-authored deadline.

    Exact refresh, admission, permit consumption and gateway dispatch each
    re-check expiry. A moving headroom requirement would silently replace the
    model-owned deadline with runner timing and is therefore not authority.
    """
    if d.action != 'OPEN':return
    require(d.open.entry_expires_at > utc(now), 'ENTRY_EXPIRED_BEFORE_ADMISSION')


def alpha_semantic_failure_code(exc):
    """Fixed diagnostics only; never relay arbitrary model/validation text."""
    return {
        'ENTRY_EXPIRED_BEFORE_ADMISSION':'ENTRY_EXPIRED_BEFORE_ADMISSION',
        'STALE_CONTEXT':'ALPHA_SEMANTIC_STALE_CONTEXT','STALE_MARKET':'ALPHA_SEMANTIC_STALE_MARKET',
        'unknown instrument':'ALPHA_SEMANTIC_UNKNOWN_INSTRUMENT','instrument symbol mismatch':'ALPHA_SEMANTIC_SYMBOL_MISMATCH',
        'price increment mismatch':'ALPHA_SEMANTIC_PRICE_INCREMENT','quantity increment mismatch':'ALPHA_SEMANTIC_QUANTITY_INCREMENT',
        'entry expiry outside regular session':'ALPHA_SEMANTIC_ENTRY_EXPIRY','invalid next review':'ALPHA_SEMANTIC_NEXT_REVIEW',
        'latest exit outside liquidation boundary':'ALPHA_SEMANTIC_EXIT_DEADLINE',
        'account state is not reconciled':'ALPHA_SEMANTIC_ACCOUNT_NOT_RECONCILED',
        'invalid session boundary':'ALPHA_SEMANTIC_SESSION_BOUNDARY',
        'duplicate instrument context':'ALPHA_SEMANTIC_DUPLICATE_INSTRUMENT',
        'invalid quote':'ALPHA_SEMANTIC_INVALID_QUOTE','OPEN outside regular session':'ALPHA_SEMANTIC_SESSION_CLOSED',
        'OPEN requires calculable positive hard loss':'ALPHA_SEMANTIC_STOP_GEOMETRY',
        'invalid long target':'ALPHA_SEMANTIC_TARGET_GEOMETRY',
        'hard loss is not calculable':'ALPHA_SEMANTIC_LOSS_NOT_CALCULABLE',
    }.get(str(exc),'ALPHA_SEMANTIC_INVALID')


def validate_alpha_semantics(d: AlphaDecision, c: AlphaContext, now: datetime,
                             *, maximum_quote_age_ms: int | None = 5000) -> AlphaDecision:
    """Validate state binding and safety geometry without judging the alpha thesis."""
    now = utc(now)
    require(maximum_quote_age_ms is None or maximum_quote_age_ms > 0, "invalid freshness policy")
    require(c.as_of <= now < c.expires_at, "STALE_CONTEXT")
    require(c.reconciliation_ready and c.settlement_known, "account state is not reconciled")
    require(c.session_open <= c.liquidation_at < c.session_close, "invalid session boundary")
    horizon = c.exit_horizon
    require(horizon >= c.liquidation_at, "invalid exit horizon")
    instruments = {item.instrument_id: item for item in c.instruments}
    require(len(instruments) == len(c.instruments), "duplicate instrument context")
    positions = {item.position_id: item for item in c.positions}
    require(len(positions) == len(c.positions), "duplicate position context")

    def instrument(instrument_id):
        item = instruments.get(instrument_id)
        require(item is not None, "unknown instrument")
        age = now - item.quote_at
        require(timedelta(0) <= age and (maximum_quote_age_ms is None or age <= timedelta(milliseconds=maximum_quote_age_ms)), "STALE_MARKET")
        require(item.bid_usd <= item.ask_usd, "invalid quote")
        return item

    def price(value, item):
        positive(value)
        require(aligned(value, item.price_increment_usd), "price increment mismatch")

    def position(position_id):
        item = positions.get(position_id)
        require(item is not None and item.quantity > 0, "unknown or flat position")
        quote = instrument(item.instrument_id)
        require(item.symbol == quote.symbol, "position instrument mismatch")
        return item, quote

    if d.action == "ABSTAIN":
        # Even no-trade decisions must be attached to a current, usable context.
        for item in c.instruments:
            instrument(item.instrument_id)
        return d
    if d.open:
        p = d.open
        require(c.session_open <= now < c.liquidation_at, "OPEN outside regular session")
        item = instrument(p.instrument_id)
        require(p.symbol == item.symbol, "instrument symbol mismatch")
        for value in (p.entry_limit_usd, p.hard_stop_usd):
            price(value, item)
        if p.target_usd is not None:
            price(p.target_usd, item)
        require(p.hard_stop_usd < p.entry_limit_usd, "OPEN requires calculable positive hard loss")
        require(p.target_usd is None or p.target_usd > p.entry_limit_usd, "invalid long target")
        require(max(now, c.session_open) < p.entry_expires_at <= c.liquidation_at, "entry expiry outside regular session")
        require(p.entry_expires_at < p.latest_exit_at <= horizon, "latest exit outside liquidation boundary")
        if p.latest_exit_at > c.session_close:
            from .carry import qualifies_for_carry
            require(qualifies_for_carry(p.entry_limit_usd, p.hard_stop_usd, p.target_usd),
                    "overnight hold requires high upside target")
        require(now < p.next_review_at <= p.latest_exit_at, "invalid next review")
        if p.desired_quantity is not None:
            require(aligned(p.desired_quantity, item.quantity_increment), "quantity increment mismatch")
            quantity = p.desired_quantity
        else:
            quantity = p.desired_notional_usd / p.entry_limit_usd
        require((p.entry_limit_usd - p.hard_stop_usd) * quantity > 0, "hard loss is not calculable")
    elif d.hold:
        require(c.session_open <= now < c.session_close, "position action outside regular session")
        p, _ = position(d.hold.position_id)
        require(now < d.hold.next_review_at <= min(p.latest_exit_at, horizon), "invalid next review")
    elif d.modify:
        require(c.session_open <= now < c.session_close, "position action outside regular session")
        m = d.modify
        p, item = position(m.position_id)
        if m.requested_stop_usd is not None:
            price(m.requested_stop_usd, item)
            require(p.active_stop_usd < m.requested_stop_usd < p.mark_usd,
                    "stop loosening or increased admitted loss")
        if m.target_action == "SET":
            price(m.requested_target_usd, item)
            require(m.requested_target_usd > p.mark_usd, "invalid long target")
        if m.target_action == "REMOVE":
            require(p.target_usd is not None, "no target exists to remove")
        if m.requested_latest_exit_at is not None:
            require(now < m.requested_latest_exit_at <= horizon, "exit deadline outside liquidation boundary")
            if m.requested_latest_exit_at > c.session_close:
                # Extending a hold past today also needs the high-upside test, measured from the current mark.
                from .carry import qualifies_for_carry
                target = m.requested_target_usd if m.target_action == "SET" else (None if m.target_action == "REMOVE" else p.target_usd)
                stop = m.requested_stop_usd if m.requested_stop_usd is not None else p.active_stop_usd
                require(qualifies_for_carry(p.mark_usd, stop, target), "overnight hold requires high upside target")
        resulting_deadline=m.requested_latest_exit_at or p.latest_exit_at
        if m.requested_next_review_at is not None:
            require(now < m.requested_next_review_at <= min(resulting_deadline, horizon), "invalid next review")
    elif d.close:
        require(c.session_open <= now < c.session_close, "position action outside regular session")
        position(d.close.position_id)
        require(d.close.full_close is True, "CLOSE must be the exact full reconciled quantity")
    return d


def require(condition: bool, message: str):
    if not condition:
        raise DecisionError(message)


def validate_semantics(d: TradeDecisionV1, c: DecisionContext, registry: SemanticRegistry,
                       now: datetime) -> TradeDecisionV1:
    now = utc(now)
    expected = {"PROPOSE_ENTRY": "entry_plan", "PROPOSE_AMENDMENT": "amendment",
                "PROPOSE_EXIT": "exit_request", "REQUEST_DATA": "data_request",
                "ABSTAIN": None, "HOLD": None}[d.action]
    for field in ("entry_plan", "amendment", "exit_request", "data_request"):
        require((getattr(d, field) is not None) == (field == expected), "action payload mismatch")
    require(d.action in c.allowed_actions, "action not allowed by context")
    bindings = ((d.decision_id, c.decision_id), (d.context_id, c.context_id),
                (d.context_hash, payload_hash(c)), (d.opportunity_id, c.opportunity_id),
                (d.portfolio_state_version, c.account.reconciliation_version),
                (d.policy_hash, c.policy_hash), (d.capability_version, c.capabilities.version),
                (d.strategy_id, c.strategy_id), (d.strategy_version, c.strategy_version),
                (d.strategy_artifact_hash, c.strategy_artifact_hash))
    require(all(a == b for a, b in bindings), "stale or mismatched context identity")
    require((d.strategy_id, d.strategy_version, d.strategy_artifact_hash) in registry.strategy_bindings,
            "unknown strategy artifact")
    require(d.policy_hash in registry.policy_hashes, "unknown policy")
    require(c.as_of <= d.created_at <= now < d.valid_until <= c.expires_at, "expired or future decision/context")
    require(c.as_of <= now, "future context")
    evidence = {e.evidence_id for e in c.evidence if e.usable_at <= c.as_of and e.received_at <= c.as_of}
    require(set(d.evidence_ids) <= evidence, "invented or future evidence")
    require(all(e.source_id in registry.source_ids for e in c.evidence), "unknown evidence source")
    features = {(f.feature_id, f.source_id): f for f in c.features if f.usable_at <= c.as_of}

    def group(g: ConditionGroup):
        require(bool(g.predicates), "empty condition group")
        for p in g.predicates:
            require(p.source_id in registry.source_ids, "unknown predicate source")
            f = features.get((p.feature_id, p.source_id))
            require(f is not None, "unknown or future feature")
            if f.value_type == "DECIMAL":
                decimal(p.value)
            elif f.value_type == "BOOLEAN":
                require(p.value in ("true", "false") and p.operator in ("EQ", "NEQ"), "invalid boolean predicate")
            else:
                require(p.value in f.allowed_values and p.operator in ("EQ", "NEQ"), "invalid enum predicate")

    def price(value):
        positive(value)
        require(aligned(value, c.instrument.price_increment_usd), "price increment mismatch")

    def targets(items):
        require(add(*(t.fraction_of_initial_quantity for t in items)) <= 1, "target fractions exceed one")
        for target in items:
            price(target.price_usd)
            positive(target.fraction_of_initial_quantity)

    def trailing(t: Trailing):
        require((t.rule_id, t.mode) in registry.trailing_rules, "unknown trailing rule")
        if t.mode == "NONE":
            require(t.activation_rule is None and t.distance is None, "NONE trailing requires null parameters")
        else:
            require(t.activation_rule is not None, "trailing activation required")
            group(t.activation_rule)
            if t.mode == "FEATURE_RULE":
                require(t.distance is None, "feature rule distance unsupported")
            else:
                positive(t.distance)
                if t.mode == "AMOUNT":
                    price(t.distance)
                else:
                    require(t.distance < 100, "percent trailing distance must be below 100")

    for g in d.review_triggers:
        group(g)
    if d.forecast:
        f = d.forecast
        quantiles = [f.net_return_quantile_10_bps, f.net_return_quantile_50_bps, f.net_return_quantile_90_bps]
        known = [q for q in quantiles if q is not None]
        require(known == sorted(known), "forecast quantiles unordered")
        if f.calibration_id is not None:
            require(f.calibration_id in registry.calibration_ids, "unknown calibration")
        if f.estimate_provenance == "QUALIFIED_ESTIMATOR":
            require(f.calibration_id is not None, "qualified estimator needs calibration reference")
    if d.data_request:
        require(d.data_request.instrument_id in (None, c.instrument.instrument_id), "unknown requested instrument")
        require(set(d.data_request.evidence_ids) <= evidence, "unknown data-request evidence")

    def readiness():
        require(registry.maximum_market_age_ms > 0 and registry.maximum_account_age_ms > 0, "invalid freshness policy")
        age = now - c.market.source_event_at
        require(timedelta(0) <= age <= timedelta(milliseconds=registry.maximum_market_age_ms), "STALE_MARKET")
        require(c.market.source_event_at <= c.market.received_at <= now, "future market receipt or clock inconsistency")
        age = now - c.account.observed_at
        require(timedelta(0) <= age <= timedelta(milliseconds=registry.maximum_account_age_ms), "STALE_ACCOUNT")
        require(c.market.entitled and c.market.delay_ms == 0 and not c.market.quality_flags and not c.warnings,
                "market/context quality unknown")
        require(c.market.instrument_id == c.instrument.instrument_id, "market instrument mismatch")
        require(c.market.bid_usd is not None and c.market.ask_usd is not None
                and 0 < c.market.bid_usd <= c.market.ask_usd, "invalid quote")
        require(c.instrument.country == "US" and c.instrument.asset_type == "STOCK"
                and c.instrument.liquid and c.instrument.tradable and not c.instrument.halted, "ineligible stock")
        require(c.session_open <= now < c.liquidation_at < c.session_close, "entry outside permitted session")

    def position(position_id, plan_id):
        matches = [p for p in c.account.positions if p.position_id == position_id and p.plan_id == plan_id]
        require(len(matches) == 1, "unknown position or plan")
        require(matches[0].instrument_id == c.instrument.instrument_id, "position instrument mismatch")
        return matches[0]

    if d.entry_plan:
        readiness()
        p = d.entry_plan
        require(p.instrument_id == c.instrument.instrument_id and p.ticker == c.instrument.ticker, "instrument mismatch")
        require(p.entry_order_type in c.capabilities.entry_order_types, "unsupported or unbounded entry order")
        price(p.maximum_entry_usd)
        price(p.limit_price_usd)
        require(p.limit_price_usd <= p.maximum_entry_usd, "limit exceeds maximum entry")
        if p.entry_order_type == "LIMIT":
            require(p.trigger_price_usd is None, "LIMIT cannot have trigger")
        else:
            price(p.trigger_price_usd)
            require(p.trigger_price_usd <= p.limit_price_usd, "stop-limit trigger exceeds limit")
        require(now < p.entry_expires_at <= d.valid_until, "entry expiry exceeds decision validity")
        require(p.entry_expires_at < p.latest_exit_at <= c.liquidation_at, "invalid entry/exit deadline")
        positive(p.sizing.amount)
        positive(p.sizing.minimum_acceptable_quantity)
        q = p.sizing.amount if p.sizing.unit == "SHARES" else floor_quantity(p.sizing.amount, p.maximum_entry_usd, c.instrument.quantity_increment)
        require(aligned(q, c.instrument.quantity_increment) and aligned(p.sizing.minimum_acceptable_quantity, c.instrument.quantity_increment), "quantity increment mismatch")
        require(q >= max(c.instrument.minimum_quantity, p.sizing.minimum_acceptable_quantity), "quantity below minimum")
        protection = p.protection
        price(protection.initial_stop_usd)
        require(protection.initial_stop_usd < min(p.limit_price_usd, c.market.bid_usd), "invalid initial stop geometry")
        require(protection.mechanism in c.capabilities.protection_mechanisms, "unsupported protection")
        require(protection.trigger_source_id in registry.source_ids, "unknown protection source")
        require(protection.execution_policy_id in registry.execution_policy_ids, "unknown execution policy")
        if protection.mechanism == "BROKER_STOP_LIMIT":
            price(protection.stop_limit_usd)
            require(protection.stop_limit_usd <= protection.initial_stop_usd, "sell stop-limit exceeds trigger")
        else:
            require(protection.stop_limit_usd is None, "unexpected stop limit")
        require(aligned(q, Decimal("1")) or c.capabilities.fractional_protection, "fractional protection uncertified")
        require(p.partial_fill_policy_id in registry.partial_fill_policy_ids, "unknown partial fill policy")
        positive(p.max_spread_bps)
        group(p.entry_condition)
        group(p.invalidation)
        targets(p.targets)
        trailing(p.trailing)
        existing = [x for x in c.account.positions if x.instrument_id == p.instrument_id and x.quantity > 0]
        require(all(x.episode_id == p.strategy_episode_id for x in existing), "add must retain existing episode")
    if d.amendment:
        readiness()
        a = d.amendment
        p = position(a.position_id, a.current_plan_id)
        require(a.current_plan_version == p.plan_version, "stale plan version")
        require(any(getattr(a, field) is not None for field in type(a).model_fields if field.startswith("requested_")), "empty amendment")
        if a.requested_stop_usd is not None:
            price(a.requested_stop_usd)
            require(p.active_stop_usd <= a.requested_stop_usd < p.mark_usd, "stop loosening or invalid trailing geometry")
        if a.requested_targets is not None:
            targets(a.requested_targets)
        if a.requested_trailing is not None:
            trailing(a.requested_trailing)
        if a.requested_invalidation is not None:
            group(a.requested_invalidation)
        if a.requested_latest_exit_at is not None:
            require(now < a.requested_latest_exit_at <= min(p.latest_exit_at, c.liquidation_at), "exit deadline extension unsupported")
    if d.action == "HOLD":
        readiness()
        require(len([p for p in c.account.positions if p.quantity > 0]) == 1, "HOLD requires one open position")
    if d.exit_request:
        readiness()
        e = d.exit_request
        p = position(e.position_id, e.current_plan_id)
        require(c.session_open <= now < c.session_close, "exit outside regular session")
        require(e.execution_policy_id in registry.execution_policy_ids, "unknown exit policy")
        if e.mode == "CLOSE":
            require(e.requested_quantity is None, "CLOSE quantity must be null")
        else:
            positive(e.requested_quantity)
            require(e.requested_quantity <= p.quantity and aligned(e.requested_quantity, c.instrument.quantity_increment), "invalid reduction quantity")
    return d
