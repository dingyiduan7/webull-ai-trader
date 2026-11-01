"""Missing optional quote prices never disable unrelated or held supervision."""
from datetime import datetime,timezone,timedelta
import pytest
from autonomous_trader.contracts import Instrument
from autonomous_trader.real_state import binding

@pytest.fixture
def market():
 from autonomous_trader import real_market
 return real_market

@pytest.mark.parametrize('quote',[{}, {'bid':'1'}, {'ask':'2'}, {'bid':None,'ask':'2'}, {'bid':'NaN','ask':'2'}, {'bid':'1','ask':'Infinity'}, {'bid':'3','ask':'2'}, {'bid':'0','ask':'2'}])
def test_invalid_optional_prices_skip_but_held_fail(market,quote):
 assert market._candidate_prices(quote) is None
 with pytest.raises(ValueError,match='INVALID_REAL_QUOTE'):market._candidate_prices(quote,held=True)

@pytest.mark.parametrize('bad_kind',['price','missing','identity'])
@pytest.mark.parametrize('held_bad,final_bad,stale_good',[(False,False,False),(True,False,False),(False,True,False),(False,False,True)])
def test_broad_refresh_preserves_good_candidate_and_real_guards(market,monkeypatch,held_bad,final_bad,stale_good,bad_kind):
 now=datetime(2026,10,6,14,tzinfo=timezone.utc);account='fixture-account'
 cash=dict(currency='USD',settled_cash='500',unsettled_cash='0',cash_balance='500',net_liquidation_value='500')
 positions=[dict(symbol='BAD',quantity='1',instrument_type='EQUITY')] if held_bad else []
 profiles={s:dict(symbol=s,instrument_id=s,status='OC',lot_size='1') for s in ('GOOD','BAD')}
 source=object.__new__(market.RealMarketSource);source.clock=lambda:now;source.account_binding=binding(account);source.market_authority=None;source.symbols=('GOOD','BAD');source.bar_cache={};source.focused=False
 calls=[0]
 class Http:
  last_received_at=now
  def read(self,request):
   path=request.get_url().split('?')[0]
   if path.endswith('/accounts/list'):return {}
   if path.endswith('/positions/list'):return positions
   if path.endswith('/open-orders/list'):return {'data':[]}
   if path.endswith('/balances/get'):return {'account_currency_assets':[cash]}
   if path.endswith('/snapshots/list'):
    calls[0]+=1
    symbols=['GOOD','BAD'] if calls[0]==1 else (['GOOD','BAD'] if final_bad else ['GOOD'])
    rows=[dict(symbol=s,instrument_id=s,bid='10',ask='10.01',list_status='LISTED',quote_time=int((now-timedelta(seconds=11 if stale_good else 0)).timestamp()*1000)) for s in symbols]
    for r in list(rows):
     if r['symbol']=='BAD' and (not final_bad or calls[0]>1):
      if bad_kind=='price':r.pop('bid')
      elif bad_kind=='missing':rows.remove(r)
      else:r['instrument_id']='wrong'
    return rows
   raise AssertionError(path)
 source.http=Http();source._profiles=lambda *a:profiles
 source._refresh_batch_bars=lambda *a:None
 source.account_observation=lambda:dict(positions=positions,cash=cash,open_client_ids=(),observed_at=now,received_at=now)
 monkeypatch.setattr(market,'select_single_individual_cash',lambda *a:{'account_id':account})
 monkeypatch.setattr(market,'listed_equity',lambda *a:True)
 monkeypatch.setattr(market,'instrument',lambda p:Instrument(instrument_id=p['symbol'],ticker=p['symbol'],country='US',asset_type='STOCK',listing_exchange='NAS',liquid=True,tradable=True,quantity_increment='1',price_increment_usd='0.01',minimum_quantity='1'))
 if held_bad:
  with pytest.raises(ValueError,match='INVALID_REAL_QUOTE|REAL_PROFILE_QUOTE_BINDING'):source.refresh()
 elif stale_good:
  with pytest.raises(ValueError,match='NO_FRESH_REAL_PORTFOLIO_CONTEXT'):source.refresh()
 else:
  packet=source.refresh();assert [i.symbol for i in packet.context.instruments]==['GOOD'];assert packet.positions=={}

