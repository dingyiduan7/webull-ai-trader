"""Read-only prerequisites for the incrementally authorized M3B sequence.

M3A's transport allowlist is unchanged. The additional SDK calendar request is
isolated here and cannot authorize a broker write.
"""
from datetime import datetime, timezone
from decimal import Decimal
import json
from urllib.parse import urlsplit
import requests
from webull.trade.request.get_trade_calendar_request import TradeCalendarRequest
from ..http import SandboxHttp, HTTP_HOST, SAFE_HEADERS
from ..quiet import quiet_sdk
from ..secrets import PreflightError
from ..http import HttpObservation


def calendar_request(day):
    request = TradeCalendarRequest()
    request.set_market("US")
    request.set_start(day.isoformat())
    request.set_end(day.isoformat())
    return request


class DiscoveryHttp(SandboxHttp):
    @quiet_sdk()
    def get_response(self, request):
        target = urlsplit(request.get_url())
        if target.path != "/trade/calendar":
            return super().get_response(request)
        query = request.get_query_params()
        if (target.scheme or target.netloc or target.fragment
                or request.get_method() != "GET" or request.get_body_params()
                or request.get_endpoint() not in (None, HTTP_HOST)
                or set(query) != {"market", "start", "end"}
                or query["market"] != "US" or query["start"] != query["end"]):
            raise PreflightError("CALENDAR_READ_REJECTED")
        try:
            datetime.strptime(query["start"], "%Y-%m-%d")
        except (ValueError, TypeError):
            raise PreflightError("CALENDAR_DATE_REJECTED") from None
        return self._send_bounded(request, target.path)

    def _send_bounded(self, request, path):
        if self._throttled:
            raise PreflightError("RATE_LIMIT_STOP_NO_RETRY")
        if self._last_request is not None:
            self._sleep(max(0, 3.2 - (self._clock() - self._last_request)))
        self._last_request = self._clock()
        wire = self._make_http_response(HTTP_HOST, request, 10, 5)
        url = "https://" + HTTP_HOST + wire.get_url()
        if urlsplit(url).hostname != HTTP_HOST:
            raise PreflightError("NON_SANDBOX_ENDPOINT_REJECTED")
        try:
            with self._session_factory() as session:
                session.trust_env = False
                prepared = session.prepare_request(requests.Request(request.get_method(), url,
                    headers=wire.get_headers(), data=wire.get_body()))
                self.contacted.append({"host": HTTP_HOST, "method": request.get_method(), "path": path})
                with session.send(prepared, allow_redirects=False, verify=True, timeout=(5, 10), stream=True) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 2_000_000:
                            raise PreflightError("RESPONSE_SIZE_LIMIT")
                        chunks.append(chunk)
                    try:
                        body = json.loads(b"".join(chunks), parse_float=Decimal,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    except (ValueError, UnicodeError):
                        body = {"non_json_response": True, "byte_count": size}
                    self._throttled = response.status_code == 429
                    result = HttpObservation(path, response.status_code,
                        datetime.now(timezone.utc).isoformat(),
                        {k.lower(): v for k, v in response.headers.items() if k.lower() in SAFE_HEADERS},
                        body, tuple(k.lower() for k in response.headers))
                    self.observations.append(result)
                    return result
        except PreflightError:
            raise
        except Exception:
            raise PreflightError("HTTP_TRANSPORT_FAILED") from None
