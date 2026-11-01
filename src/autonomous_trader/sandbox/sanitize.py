"""Preserve response structure; retain only reviewed nonpersonal scalar values."""
from decimal import Decimal
import json
import re
from .secrets import PreflightError

PRIVATE = re.compile(r"account|userid|customer|clientorder|orderid|requestid|sessionid|pagination|cursor|phone|email|address|ssn|taxid|fullname|firstname|lastname")
SECRET = re.compile(r"appkey|appsecret|token|authorization|authentication|signature|password|credential|cookie|apikey|secret")
NUMERIC = set("cash total_cash settled_cash unsettled_cash buying_power day_buying_power overnight_buying_power net_liquidation net_liquidation_value total_asset total_assets market_value unrealized_profit_loss realized_profit_loss unrealized_pnl realized_pnl cash_balance available_cash withdrawable_cash frozen_cash pending_cash accrued_interest quantity qty total_quantity available_quantity sellable_quantity cost_price avg_cost price open high low close pre_close volume size amount balance net_asset equity price_increment quantity_increment min_quantity tick_size lot_size min_tick decimal_places precision count timestamp time trade_time update_time delay delay_seconds delay_millis delay_ms delayed_minutes available_balance cash_available equity_value position_value total_market_value unfilled_quantity filled_quantity remaining_quantity last_price bid ask bid_size ask_size avg_price trade_timestamp server_time".split())
ENUM_FIELDS = set("account_type account_status type status currency base_currency total_asset_currency category sub_category market exchange symbol ticker instrument_id security_type asset_type direction side order_type time_in_force trading_session session entitlement permission quote_permission source source_id feed data_source delay_type error_code code".split())
ENUM_VALUES = set("CASH MARGIN INDIVIDUAL ACTIVE NORMAL OPEN CLOSED PENDING WORKING FILLED PARTIALLY_FILLED CANCELED CANCELLED REJECTED EXPIRED USD US US_STOCK STOCK EQUITY COMMON_STOCK NASDAQ NYSE ARCA BATS AAPL REALTIME REAL_TIME DELAYED DELAY L1 L2 QUOTE SNAPSHOT TICK BUY SELL LIMIT MARKET DAY CORE REGULAR ALL NONE UNKNOWN TRUE FALSE".split())
NUMERIC.update("last_trade_time quote_time open_time close_time change change_ratio drop rtt sent".split())
NUMERIC.update("total_net_liquidation_value total_cash_balance total_unrealized_profit_loss total_day_profit_loss option_buying_power night_trading_buying_power day_profit_loss".split())
ENUM_VALUES.add("STATUS")


def normalize(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


class Sanitizer:
    def __init__(self, secrets=()):
        self._secrets = tuple(x for x in secrets if x)
        self._aliases = {}

    def _contains_secret(self, value):
        text = str(value)
        return any(secret in text for secret in self._secrets)

    def clean(self, value, key=""):
        norm = normalize(key)
        if SECRET.search(norm):
            return "<redacted:credential>"
        if PRIVATE.search(norm) and value is not None and not isinstance(value, (dict, list)):
            if norm in ("accounttype", "accountstatus", "accountclass"):
                pass
            else:
                token = str(value)
                if not token:
                    return ""
                if token not in self._aliases:
                    self._aliases[token] = f"private-{len(self._aliases) + 1:03d}"
                return self._aliases[token]
        if isinstance(value, dict):
            return {k if not self._contains_secret(k) else "redacted_key": self.clean(v, k)
                    for k, v in value.items() if not SECRET.search(normalize(k))}
        if isinstance(value, (tuple, list)):
            return [self.clean(v, key) for v in value]
        if value is None or isinstance(value, bool):
            return value
        if self._contains_secret(value):
            return "<redacted:known-secret>"
        if norm in ("accountclass", "exchangecode", "tradingsession", "liststatus", "status") and re.fullmatch(r"[A-Z0-9_]{1,32}", str(value)):
            return value  # product/exchange/session/status codes, never account identifiers
        if norm in {normalize(k) for k in NUMERIC} and re.fullmatch(r"-?\d+(?:\.\d+)?", str(value)):
            return str(value) if isinstance(value, Decimal) else value
        if norm in {normalize(k) for k in NUMERIC} and re.fullmatch(r"\d{4}-\d\d-\d\d[T ][\d:.+-]+Z?", str(value)):
            return value
        if norm in {normalize(k) for k in ENUM_FIELDS} and (str(value).upper() in ENUM_VALUES or
                (key == "error_code" and re.fullmatch(r"[A-Z_]{1,50}", str(value)))):
            return value
        if key == "instrument_id" and re.fullmatch(r"[A-Za-z0-9.-]{1,40}", str(value)):
            return value
        return "<redacted:" + ("number" if isinstance(value, (int, float, Decimal)) else "string") + ">"

    def encode(self, payload):
        output = json.dumps(self.clean(payload), indent=2, ensure_ascii=True, allow_nan=False)
        self.assert_clean(output)
        return output

    def assert_clean(self, text):
        if self._contains_secret(text):
            raise PreflightError("SANITIZATION_FAILED")


def shape(value):
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        # Do not infer an element schema from an empty returned collection.
        return {"type": "array", "count": len(value), "observed_items": [shape(v) for v in value[:3]]}
    return "null" if value is None else "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float, Decimal)) else "string"
