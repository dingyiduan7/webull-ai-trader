"""Owner approval 2026-10-21: preview-only fractional order test. The tool can only reach /trading/orders/preview."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fractional_preview as F


def test_only_limit_orders_with_sane_quantities():
    good = F.order('SPY', 'BUY', '0.01', '500.00')
    assert F.preview_body('acct', good) == {'account_id': 'acct', 'new_orders': [good]}
    for bad in (dict(good, order_type='MARKET'), dict(good, side='SHORT'), dict(good, quantity='-1'),
                dict(good, quantity='1e9'), dict(good, stop_price='1')):
        with pytest.raises(ValueError):
            F.preview_body('acct', bad)


def test_request_goes_only_to_the_preview_endpoint(monkeypatch):
    sent = []
    class Wire:
        def get_url(self):return '/trading/orders/preview'
        def get_headers(self):return {}
        def get_body(self):return '{}'
    class Client:
        def _make_http_response(self, host, request, *a):
            sent.append(request.get_url());return Wire()
    class Session:
        trust_env = True
        def __enter__(self):return self
        def __exit__(self, *a):pass
        def prepare_request(self, r):sent.append(r.url);return r
        def send(self, *a, **k):return SimpleNamespace(status_code=200, json=lambda: {'ok': True}, text='')
    import requests
    monkeypatch.setattr(requests, 'Session', Session)
    status, payload = F.send_preview(SimpleNamespace(client=Client()), 'acct', F.order('SPY', 'BUY', '0.01', '1.00'))
    assert status == 200 and all(url.endswith('/trading/orders/preview') for url in sent)
    assert 'place' not in ' '.join(sent)
