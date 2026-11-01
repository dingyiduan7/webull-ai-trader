"""Fractional-share PREVIEW test (owner approval 2026-10-21: "I approve a preview-only fractional order test, then
fractional buys and sells if Webull accepts fractional limit orders").

PREVIEW ONLY: every request goes to /trading/orders/preview, which validates an order and estimates its cost
without placing it. The path is asserted before each send; no other write endpoint is reachable from here.
Reads used: account list (to find the cash account) and one quote snapshot per symbol (to set a limit price).

Usage:  .venv\\Scripts\\python.exe scripts\\fractional_preview.py
Writes: .local/real-readiness/fractional-preview.json
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]

PREVIEW_PATH = '/trading/orders/preview'
ORDER_KEYS = {'client_order_id', 'combo_type', 'symbol', 'instrument_type', 'market', 'order_type', 'quantity',
              'side', 'time_in_force', 'support_trading_session', 'entrust_type', 'limit_price'}


def preview_body(account_id, order):
    """The only request shape this tool sends: one LIMIT order to the preview endpoint."""
    if set(order) - ORDER_KEYS or order.get('order_type') != 'LIMIT' or order.get('side') not in ('BUY', 'SELL'):
        raise ValueError('PREVIEW_ORDER_SHAPE')
    if not re.fullmatch(r'\d{1,6}(\.\d{1,6})?', order['quantity']) or Decimal(order['quantity']) <= 0:
        raise ValueError('PREVIEW_QUANTITY')
    return {'account_id': account_id, 'new_orders': [order]}


def send_preview(http, account_id, order):
    from webull.trade.request.v3.preview_order_request import PreviewOrderRequest
    import requests
    request = PreviewOrderRequest()
    body = preview_body(account_id, order)
    request.set_account_id(body['account_id']);request.set_new_orders(body['new_orders'])
    path = urlsplit(request.get_url()).path
    if path != PREVIEW_PATH or request.get_method() != 'POST':
        raise ValueError('PREVIEW_PATH_REQUIRED')          # never anything but the preview endpoint
    wire = http.client._make_http_response('api.webull.com', request, 10, 5)
    url = 'https://api.webull.com' + wire.get_url()
    if urlsplit(url).path != PREVIEW_PATH:
        raise ValueError('PREVIEW_PATH_REQUIRED')
    with requests.Session() as session:
        session.trust_env = False
        prepared = session.prepare_request(requests.Request('POST', url, headers=wire.get_headers(), data=wire.get_body()))
        response = session.send(prepared, allow_redirects=False, verify=True, timeout=(5, 10))
    try:
        payload = response.json()
    except ValueError:
        payload = {'non_json': response.text[:300]}
    return response.status_code, payload


def order(symbol, side, quantity, limit, entrust='QTY'):
    return dict(client_order_id=uuid4().hex, combo_type='NORMAL', symbol=symbol, instrument_type='EQUITY', market='US',
                order_type='LIMIT', quantity=str(quantity), side=side, time_in_force='DAY',
                support_trading_session='CORE', entrust_type=entrust, limit_price=str(limit))


def main():
    from autonomous_trader.real_secrets import load_real_credentials
    from autonomous_trader.real_strategy_broker import RealStrategyHttp
    from autonomous_trader.real_preflight import select_single_individual_cash
    from autonomous_trader.real_market import snapshot_request
    from autonomous_trader.sandbox.http import build_request, Operation
    http = RealStrategyHttp(load_real_credentials(ROOT))
    account_id = select_single_individual_cash(http.read(build_request(Operation.ACCOUNTS)))['account_id']
    quotes = {q['symbol']: q for q in http.read(snapshot_request(('F', 'SPY')))}
    def below_bid(symbol):                             # a limit below the bid: valid, and could not fill anyway
        bid = Decimal(str(quotes[symbol]['bid']))
        return (bid * Decimal('0.97')).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    cases = [
        ('control_whole_share_buy', order('F', 'BUY', 1, below_bid('F'))),
        ('fractional_limit_buy', order('SPY', 'BUY', '0.01', below_bid('SPY'))),
        ('fractional_limit_buy_tiny', order('SPY', 'BUY', '0.001', below_bid('SPY'))),
        ('fractional_limit_sell_not_held', order('SPY', 'SELL', '0.01', below_bid('SPY'))),
    ]
    results = []
    for name, o in cases:
        try:
            status, payload = send_preview(http, account_id, o)
        except Exception as exc:
            status, payload = None, {'error': type(exc).__name__ + ':' + str(exc)[:160]}
        text = json.dumps(payload, default=str)
        results.append(dict(case=name, symbol=o['symbol'], side=o['side'], quantity=o['quantity'],
                            limit=o['limit_price'], http_status=status, accepted=status == 200, response=text[:600]))
        print('%-32s HTTP %s  %s' % (name, status, text[:160]), flush=True)
    out = ROOT / '.local' / 'real-readiness' / 'fractional-preview.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(at=datetime.now(timezone.utc).isoformat(), preview_only=True, results=results),
                              indent=1), encoding='utf-8')
    print('saved', out)


if __name__ == '__main__':
    main()
