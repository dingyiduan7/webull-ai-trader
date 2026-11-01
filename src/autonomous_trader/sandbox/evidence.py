"""Sanitized observations are evidence, never account-state or risk authority."""
from datetime import datetime, timezone
from pathlib import Path
import json
import re
from decimal import Decimal, InvalidOperation
from .sanitize import Sanitizer, SECRET, normalize, shape
from .secrets import PreflightError


def account_rows(body):
    rows = body if isinstance(body, list) else body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
        raise PreflightError("UNRECOGNIZED_ACCOUNT_LIST_SHAPE")
    return rows


def select_account(body, designated_id):
    if not designated_id:
        raise PreflightError("OWNER_ACCOUNT_DESIGNATION_REQUIRED")
    matches = [a for a in account_rows(body) if a.get("account_id") == designated_id]
    if len(matches) != 1:
        raise PreflightError("DESIGNATED_ACCOUNT_NOT_UNIQUELY_RETURNED")
    if matches[0].get("account_type") != "CASH":
        raise PreflightError("CASH_ACCOUNT_TYPE_NOT_CONFIRMED")
    return matches[0]


def fixture(observation, sanitizer):
    body = sanitizer.clean(observation.body)
    headers = {}
    for name, value in observation.headers.items():
        if name == "date" and re.fullmatch(r"[A-Za-z]{3}, \d{2} [A-Za-z]{3} \d{4} [\d:]{8} GMT", value):
            headers[name] = value
        elif name == "content-type" and re.fullmatch(r"application/json(?:; ?charset=[\w-]+)?", value, re.I):
            headers[name] = value
        elif re.fullmatch(r"\d+(?:\.\d+)?", value):
            headers[name] = value
        else:
            headers[name] = "<redacted:header-value>"
    # Type shapes carry no scalar values. Sensitive field names are excluded.
    def safe_shape(value):
        if isinstance(value, dict):
            return {k: safe_shape(v) for k, v in value.items() if not SECRET.search(normalize(k))}
        if isinstance(value, list):
            return {"type": "array", "count": len(value), "observed_items": [safe_shape(v) for v in value[:3]]}
        return shape(value)
    result = dict(origin="OBSERVED_SANDBOX_HTTP", host="api.sandbox.webull.com", path=observation.path,
        status_code=observation.status_code, received_at=observation.received_at,
        headers=headers, body=body, original_value_types=safe_shape(observation.body),
        omitted_scalar_policy="Unreviewed values redacted; absent fields are never synthesized")
    result["nonprivate_header_names"] = [k for k in observation.header_names
        if re.fullmatch(r"[a-z][a-z0-9-]{1,64}", k) and not SECRET.search(normalize(k))
        and not any(part in normalize(k) for part in ("requestid", "session", "user", "account", "cookie"))]
    if observation.status_code >= 400 and isinstance(observation.body, dict):
        # Classify error text in memory without retaining free text that could
        # echo account IDs, tokens, request signatures or other personal data.
        text = " ".join(str(observation.body.get(k, "")) for k in ("message", "error_msg")).lower()
        result["error_topics"] = [label for label, terms in {
            "SIGNATURE": ("signature", "sign check"), "TOKEN": ("token",),
            "APP_KEY": ("app key", "app_key", "appkey"), "PERMISSION": ("permission", "access denied"),
            "EXPIRED": ("expired", "expiry"), "TIMESTAMP": ("timestamp", "clock"),
            "PATH": ("path", "route", "not found"), "UNAUTHORIZED": ("unauthorized", "not authorized"),
            "CREDENTIALS": ("credential", "authentication", "authenticate"),
            "MISSING": ("missing", "required", "not provided"),
            "MISMATCH": ("mismatch", "not match", "does not match"),
            "INVALID": ("invalid", "incorrect", "failed", "failure"),
            "UNSUPPORTED": ("unsupported", "not supported"),
        }.items() if any(term in text for term in terms)]
    sanitizer.assert_clean(json.dumps(result))
    return result


def reconciliation_assessment(observations):
    # No name-based conversion from buying_power/cash_balance to eligible settled
    # funds. The M2 environment stays OFFLINE and receives no broker commands.
    return {"environment": "SANDBOX_OBSERVATION", "can_reconcile_automatically": False,
        "can_admit_risk": False, "reasons": ["SETTLEMENT_FIELD_SEMANTICS_NOT_CERTIFIED",
            "BROKER_TO_INTERNAL_INSTRUMENT_AND_ORDER_BINDINGS_NOT_CERTIFIED",
            "M2_OFFLINE_AUTHORITY_NOT_RECONFIGURED"], "observation_count": len(observations)}


def market_timing_observation(observations, mqtt):
    """Describe timestamp ages; thresholds classify this sample, not risk policy."""
    samples = []
    def add(source, field, timestamp, received):
        if not re.fullmatch(r"\d{13}", str(timestamp)):
            return
        try:
            received_time = datetime.fromisoformat(received)
            if received_time.tzinfo is None:
                return
            delta = received_time - datetime(1970, 1, 1, tzinfo=timezone.utc)
            seconds = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(1000000)
            age = seconds - Decimal(str(timestamp)) / Decimal(1000)
        except (ValueError, TypeError, InvalidOperation):
            return
        samples.append({"source": source, "field": field, "event_epoch_ms": str(timestamp),
                        "received_at": received, "age_seconds": str(age)})
    for observation in observations:
        if observation.path == "/market-data/stocks/snapshots/list" and observation.status_code == 200 and isinstance(observation.body, list):
            for row in observation.body:
                if isinstance(row, dict) and row.get("symbol") == "AAPL":
                    for field in ("last_trade_time", "quote_time"):
                        add("HTTP_SNAPSHOT", field, row.get(field), observation.received_at)
    for attempt in mqtt.get("attempts", []):
        for message in attempt.get("messages", []):
            payload = message.get("payload", {})
            if not isinstance(payload, dict) or not isinstance(payload.get("basic"), dict):
                continue
            if payload["basic"].get("symbol") != "AAPL":
                continue
            if message["topic"] == "quote":
                add("MQTT_QUOTE", "basic.timestamp", payload["basic"].get("timestamp"), message.get("received_at", ""))
            elif message["topic"] == "snapshot":
                add("MQTT_SNAPSHOT", "trade_time", payload.get("trade_time"), message.get("received_at", ""))
    ages = [Decimal(x["age_seconds"]) for x in samples]
    finding = "UNDETERMINED"
    if ages and all(Decimal(-2) <= age <= Decimal(10) for age in ages):
        finding = "OBSERVED_NEAR_REAL_TIME_FOR_AAPL_SAMPLE"
    elif ages and all(Decimal(850) <= age <= Decimal(950) for age in ages):
        finding = "OBSERVED_APPROXIMATELY_15_MINUTE_DELAY_FOR_AAPL_SAMPLE"
    elif ages:
        finding = "MIXED_OR_OTHER_TIMESTAMP_AGES_REQUIRES_REVIEW"
    return {"finding": finding, "samples": samples,
        "scope": "Timestamp observation only; not an entitlement package guarantee or executable freshness approval",
        "negative_age_note": "Negative ages expose clock/receipt ordering uncertainty; they are not clamped to zero"}


def save_json(path: Path, value, sanitizer):
    text = json.dumps(value, indent=2, allow_nan=False, ensure_ascii=True) + "\n"
    sanitizer.assert_clean(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
