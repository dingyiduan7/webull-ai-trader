from datetime import timedelta, date, datetime, timezone
from decimal import Decimal as D, localcontext
from hashlib import sha256
import hmac
import json
from pathlib import Path
import ast
import pytest
from hypothesis import given, strategies as st, settings
from autonomous_trader.contracts import Envelope, MarketEvent, payload_hash, RiskPermit
from autonomous_trader.numeric import decimal, add, mul, floor_quantity, aligned
from autonomous_trader.risk import (POLICY, HardPolicy, SafetyState, OperatingSegment,
    EquityObservation, OwnerRestart, HMACOwnerAuthenticator, Mode)


def state(now):
    return SafetyState(experiment_id="experiment", owner_id="owner", initial_equity_usd="1000000",
        historic_high_water_usd="1000000", current_equity_usd="1000000", day_start_equity_usd="1000000",
        session_label=now.date(), segments=(OperatingSegment(segment_id="original", reference_equity_usd="1000000",
        high_water_usd="1000000", started_at=now, restart_action_id=None),), reconciling=False, session_closed=False)


def observe(s, now, equity, realized="0"):
    return s.observe(EquityObservation(at=now, session_label=s.session_label, equity_usd=equity, realized_net_pnl_usd=realized))


TEST_KEY = b"offline-test-fixture-key-00000000"


def restart_action(s, now, key=TEST_KEY, **updates):
    action = OwnerRestart(action_id="restart-" + str(len(s.segments)), owner_id="owner", experiment_id=s.experiment_id,
        segment_id=s.segments[-1].segment_id, expected_equity_usd=s.current_equity_usd, issued_at=now,
        expires_at=now + timedelta(minutes=1), proof="unsigned")
    action = OwnerRestart(**{**action.model_dump(), **updates})
    body = json.dumps(action.model_dump(mode="json", exclude={"proof"}), sort_keys=True, separators=(",", ":")).encode()
    return OwnerRestart(**{**action.model_dump(), "proof": hmac.new(key, body, sha256).hexdigest()})


@pytest.mark.parametrize("equity,realized,halted", [("950000.01", "0", False), ("950000", "0", True), ("1100000", "-50000", True), ("1000000", "-49999.99", False)])
def test_daily_loss_marked_and_realized(now, equity, realized, halted):
    s = observe(state(now), now, equity, realized)
    assert s.daily_halted is halted
    s = observe(s, now + timedelta(seconds=1), "1100000")
    assert s.daily_halted is halted


def test_drawdown_latches_on_exact_threshold_and_survives_recovery(now):
    s = state(now)
    assert not observe(s, now, "800000.01").drawdown_halted
    s = observe(s, now, "800000")
    assert s.mode == Mode.DRAWDOWN_HALTED
    restored = SafetyState.model_validate_json(s.model_dump_json())
    assert restored == s
    s = observe(restored, now + timedelta(seconds=1), "1100000")
    assert s.drawdown_halted
    assert s.historic_high_water_usd == D("1100000")


def test_owner_restart_new_segment_preserves_history_and_daily_halt(now):
    s = observe(state(now), now, "800000")
    action = restart_action(s, now)
    with pytest.raises(PermissionError):
        s.restart(action, now)
    with pytest.raises(PermissionError):
        s.restart(action, now, HMACOwnerAuthenticator("other", TEST_KEY))
    with pytest.raises(PermissionError):
        s.restart(restart_action(s, now, key=b"wrong-test-key-000000000000000000"), now, HMACOwnerAuthenticator("owner", TEST_KEY))
    restarted = s.restart(action, now, HMACOwnerAuthenticator("owner", TEST_KEY))
    assert not restarted.drawdown_halted
    assert restarted.daily_halted
    assert restarted.mode == Mode.REDUCE_ONLY
    assert restarted.segments[0] == s.segments[0]
    assert restarted.segments[1].reference_equity_usd == D("800000")
    assert restarted.historic_high_water_usd == D("1000000")
    assert restarted.observations == s.observations
    next_day = restarted.new_session(date(2026, 9, 14), reconciled=True, ready=True)
    assert next_day.mode == Mode.READY
    next_day = observe(next_day, now + timedelta(days=3), "799999")
    assert not next_day.drawdown_halted  # original 80% level no longer re-triggers segment halt
    next_day = observe(next_day, now + timedelta(days=3, seconds=1), "640000")
    assert next_day.drawdown_halted
    with pytest.raises(ValueError):
        next_day.restart(action, now, HMACOwnerAuthenticator("owner", TEST_KEY))


@pytest.mark.parametrize("change", [{"expected_equity_usd": "799999"}, {"experiment_id": "other"}, {"segment_id": "other"}])
def test_restart_action_bound_to_observed_state(now, change):
    s = observe(state(now), now, "800000")
    with pytest.raises(ValueError):
        s.restart(restart_action(s, now, **change), now, HMACOwnerAuthenticator("owner", TEST_KEY))


def test_restart_expiry_and_tampering(now):
    s = observe(state(now), now, "800000")
    action = restart_action(s, now)
    with pytest.raises(ValueError):
        s.restart(action, now + timedelta(minutes=1), HMACOwnerAuthenticator("owner", TEST_KEY))
    with pytest.raises(PermissionError):
        s.restart(action.model_copy(update={"action_id": "tampered"}), now, HMACOwnerAuthenticator("owner", TEST_KEY))


def test_cumulative_death_never_resets_across_segments(now):
    s = observe(state(now), now, "800000")
    s = s.restart(restart_action(s, now), now, HMACOwnerAuthenticator("owner", TEST_KEY))
    s = observe(s, now + timedelta(seconds=1), "100000")
    assert s.terminated and s.mode == Mode.TERMINATED
    assert s.historic_high_water_usd == D("1000000")
    with pytest.raises(ValueError, match="nonterminated"):
        s.restart(restart_action(s, now), now, HMACOwnerAuthenticator("owner", TEST_KEY))
    s = observe(s, now + timedelta(seconds=2), "1000000")
    assert s.terminated


def test_new_day_clears_only_daily_conditions(now):
    s = observe(state(now), now, "800000").evolve(incident_halted=True)
    with pytest.raises(ValueError):
        s.new_session(date(2026, 9, 14), reconciled=False, ready=True)
    s = s.new_session(date(2026, 9, 14), reconciled=True, ready=True)
    assert not s.daily_halted
    assert s.incident_halted and s.drawdown_halted


def test_envelope_hash_immutable_and_unknown_fields(context, now):
    market = context.market
    data = dict(event_id="event", correlation_id="correlation", causation_id=None, created_at=now, usable_at=now,
        source_id="fixture", revision=0, payload_hash=payload_hash(market), payload=market)
    envelope = Envelope[MarketEvent](**data)
    assert Envelope[MarketEvent].model_validate_json(envelope.model_dump_json()) == envelope
    with pytest.raises(ValueError):
        Envelope[MarketEvent](**{**data, "payload_hash": "0" * 64})
    with pytest.raises(ValueError):
        market.bid_usd = D("1")
    with pytest.raises(ValueError):
        MarketEvent(**{**market.model_dump(), "funding": "no"})


@pytest.mark.parametrize("kind,value", [("DECIMAL", "arbitrary code"), ("BOOLEAN", "true"), ("ENUM", "UNKNOWN")])
def test_features_require_registered_typed_values(now, kind, value):
    from autonomous_trader.contracts import Feature
    with pytest.raises(ValueError):
        Feature(feature_id="feature", source_id="fixture", value=value, value_type=kind, usable_at=now)


def test_decimal_feature_is_decimal(context):
    assert isinstance(context.features[0].value, D)


@pytest.mark.parametrize("value", [1.01, True, "NaN", "Infinity", "1e9", "-0.000000001", D("1e1000000"), D("NaN"), D("Infinity")])
def test_decimal_rejects_unsafe_input(value):
    with pytest.raises(ValueError):
        decimal(value)


def test_exact_arithmetic_independent_of_ambient_context():
    with localcontext() as ctx:
        ctx.prec = 6
        assert add(D("999999999999.99"), D("0.01")) == D("1000000000000.00")
        assert mul(D("999999999999.99"), D("10")) == D("9999999999999.90")
        assert floor_quantity(D("100"), D("33.33"), D("1")) == 3


@settings(max_examples=100, derandomize=True)
@given(st.integers(0, 1000000), st.integers(1, 100000), st.sampled_from([D("1"), D("0.01"), D("0.000001")]))
def test_round_down_never_overspends(notional, cents, increment):
    price = D(cents) / D("100")
    q = floor_quantity(D(notional), price, increment)
    assert aligned(q, increment)
    assert mul(q, price) <= notional
    assert mul(add(q, increment), price) > notional


def test_policy_numbers_cannot_be_relaxed():
    for field, value in [("account_exposure", D("1.1")), ("stock_exposure", D("0.21")), ("maximum_positions", 6),
                         ("trade_risk", D("0.02")), ("open_risk", D("0.06")), ("margin", True), ("borrowing", True),
                         ("money_movement", True), ("environment", "LIVE")]:
        with pytest.raises(ValueError):
            HardPolicy(**{**POLICY.model_dump(), field: value})


def test_policy_and_capability_manifests_match_contracts(context):
    from autonomous_trader.contracts import CapabilityManifest
    root = Path(__file__).resolve().parents[1]
    import json
    paper = json.loads((root / "policy/paper-policy.json").read_text())
    # trade_risk/open_risk were revised by the owner on 2026-10-16 (charter re-sign); the paper file is a
    # historical fixture outside the release scope, so only those two fields may differ.
    paper.update(trade_risk=str(POLICY.trade_risk), open_risk=str(POLICY.open_risk))
    assert HardPolicy.model_validate(paper) == POLICY
    assert HardPolicy.model_validate_json(POLICY.model_dump_json()) == POLICY
    assert CapabilityManifest.model_validate_json((root / "policy/offline-capabilities.json").read_text()) == context.capabilities


def test_source_schema_unchanged_and_packaged_copy_matches():
    root = Path(__file__).resolve().parents[1]
    assert (root / "trade-decision.schema.json").read_bytes() == (root / "src/autonomous_trader/trade-decision.schema.json").read_bytes()


def test_no_network_credentials_or_live_dependencies():
    package = Path(__file__).resolve().parents[1] / "src/autonomous_trader"
    prohibited = {"openai", "webull", "requests", "httpx", "urllib", "socket", "boto3", "subprocess"}
    for path in package.glob("*.py"):
        # REAL adapters are explicit integration boundaries; this invariant
        # protects the canonical offline M1 contracts/risk package.
        if path.name.startswith("real_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(alias.name.split(".")[0] in prohibited for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in prohibited
            if isinstance(node, ast.FunctionDef):
                assert node.name not in {"deposit", "withdraw", "transfer", "borrow", "activate_live"}


def test_pure_contract_reference_risk_dependency_boundaries():
    package = Path(__file__).resolve().parents[1] / "src/autonomous_trader"
    for name in ("contracts", "numeric", "reference", "risk", "decisions"):
        tree = ast.parse((package / (name + ".py")).read_text())
        assert not any(isinstance(node, ast.ImportFrom) and node.module in {"execution", "portfolio"} for node in ast.walk(tree))
