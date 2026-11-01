"""Fail-closed, finite certification prerequisites. Never a live policy."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from ...risk import POLICY
from ..evidence import select_account
from ..secrets import PreflightError

MODE = "SANDBOX_CERTIFICATION"
DAY = "2026-09-11"
# Explicit one-day calendar evidence: docs/M3B_PLAN.md, official exchange calendar.
# Other dates require new reviewed evidence, not a weekday guess.


def regular_session(now):
    if now.tzinfo is None:
        raise PreflightError("NAIVE_CLOCK")
    local = now.astimezone(ZoneInfo("America/New_York"))
    if local.date().isoformat() != DAY or not (9 * 60 + 30 <= local.hour * 60 + local.minute < 15 * 60 + 55):
        raise PreflightError("OUTSIDE_CERTIFIED_SESSION")


def exact_decimal(value):
    import re
    if not isinstance(value, str) or not re.fullmatch(r"-?\d{1,18}(?:\.\d{1,10})?", value):
        raise PreflightError("UNRECOGNIZED_NUMERIC_FIELD")
    return Decimal(value)


@dataclass(frozen=True)
class FlatPrerequisites:
    account_id: str = field(repr=False)
    received_at: datetime
    equity: Decimal
    settled: Decimal


def verify_flat(mode, account_id, observations, now):
    if mode != MODE:
        raise PreflightError("EXPLICIT_CERTIFICATION_MODE_REQUIRED")
    regular_session(now)
    if any(o.status_code != 200 for o in observations.values()):
        raise PreflightError("PREREQUISITE_HTTP_FAILURE")
    if any(not 0 <= (now - datetime.fromisoformat(o.received_at)).total_seconds() <= 30
           for o in observations.values()):
        raise PreflightError("STALE_OR_FUTURE_PREREQUISITES")
    account = select_account(observations["accounts"].body, account_id)
    if account.get("account_class") != "INDIVIDUAL_CASH":
        raise PreflightError("INDIVIDUAL_CASH_NOT_CONFIRMED")
    if observations["positions"].body != [] or observations["orders"].body != {"data": []}:
        raise PreflightError("NOT_FLAT_OR_UNKNOWN_OBLIGATIONS")
    balance = observations["balance"].body
    assets = balance.get("account_currency_assets") if isinstance(balance, dict) else None
    if not isinstance(assets, list) or len(assets) != 1 or assets[0].get("currency") != "USD":
        raise PreflightError("UNRECOGNIZED_CASH_SHAPE")
    cash = assets[0]
    equity, settled = exact_decimal(cash["net_liquidation_value"]), exact_decimal(cash["settled_cash"])
    if (exact_decimal(cash["unsettled_cash"]) != 0 or exact_decimal(cash["market_value"]) != 0
            or exact_decimal(cash["cash_balance"]) != settled or equity != settled):
        raise PreflightError("CASH_RECONCILIATION_REQUIRED")
    # Historical $1m M3A flat account is not reset by a new invocation.
    reference = Decimal("1000000")
    if equity < reference or exact_decimal(cash["day_profit_loss"]) != 0:
        raise PreflightError("ACCOUNT_CHANGED_SINCE_M3A_REQUIRES_RECONCILIATION")
    return FlatPrerequisites(account_id, now, equity, min(settled, reference))


def validate_single_order(order, prerequisites, now):
    regular_session(now)
    if not 0 <= (now - prerequisites.received_at).total_seconds() <= 5:
        raise PreflightError("PREREQUISITES_EXPIRED")
    fixed = {"symbol": "AAPL", "instrument_type": "EQUITY", "market": "US",
        "combo_type": "NORMAL", "order_type": "LIMIT", "quantity": "1", "side": "BUY",
        "time_in_force": "DAY", "support_trading_session": "CORE", "entrust_type": "QTY"}
    if set(order) != set(fixed) | {"client_order_id", "limit_price"} or any(order[k] != v for k,v in fixed.items()):
        raise PreflightError("ONE_SHARE_CORE_LIMIT_ONLY")
    import re
    if not re.fullmatch(r"[a-f0-9]{32}", order["client_order_id"]):
        raise PreflightError("PERSISTENT_CLIENT_ID_REQUIRED")
    price = exact_decimal(order["limit_price"])
    if not Decimal("1") <= price <= Decimal("1000") or price % Decimal("0.01"):
        raise PreflightError("CERTIFICATION_PRICE_BOUND")
    # Full principal plus $25 reserve is stricter than any stop-based loss
    # measure for this one-share PREVIEW. It is not a fee schedule or permission
    # to place an unprotected order. No execution capability is granted here.
    worst = price + Decimal("25")
    base = min(prerequisites.equity, Decimal("1000000"))
    if (worst > prerequisites.settled or price > prerequisites.equity * POLICY.stock_exposure
            or price > prerequisites.equity * POLICY.account_exposure
            or worst > base * POLICY.trade_risk or worst > base * POLICY.open_risk):
        raise PreflightError("HARD_RISK_BOUND")
