"""Reviewed M3B order/fee scalars, retaining M3A secret and identifier rules."""
import re
from decimal import Decimal
from ..sanitize import Sanitizer, normalize

NUMBERS = set("limitprice stopprice filledqty filledquantity filledprice avgfilledprice averagefilledprice totalfilledquantity remainingqty remainingquantity estimatedcost estimatedtransactionfee commission fees fee totalfee transactionfee executionprice executedquantity executedprice orderquantity canceledquantity cancelledquantity".split())
NUMBERS.update("placetime filledtime canceltime updatetime actualvalue receivablevalue actualcommission receivablecommission".split())
TIMES = {"placetimeat", "filledtimeat", "canceltimeat", "updatetimeat", "filledtime"}
CODES = set("combotype instrumenttype supporttradingsession entrusttype orderstatus scenetype tradestatus".split())


class OrderSanitizer(Sanitizer):
    def clean(self, value, key=""):
        norm = normalize(key)
        if norm in {"executionid", "tradeid", "positionid"} and isinstance(value, (str, int)):
            token = str(value)
            if token not in self._aliases:
                self._aliases[token] = f"private-{len(self._aliases) + 1:03d}"
            return self._aliases[token]
        if not self._contains_secret(value):
            if norm in TIMES and isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.+-]+Z?", value):
                return value
            if norm in NUMBERS and isinstance(value, (str, int, Decimal)) and re.fullmatch(r"-?\d+(?:\.\d+)?", str(value)):
                return str(value) if isinstance(value, Decimal) else value
            if norm in CODES and isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,40}", value):
                return value
        return super().clean(value, key)
