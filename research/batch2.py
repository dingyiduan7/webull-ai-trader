"""Batch 2 (pre-registered 2026-10-22):
12 volatility-managed SPY (Moreira-Muir): exposure = 15% target / 20-day realized vol, capped at 2x, daily
3p earnings-gap drift proxy: gap up >= 5% at the open on >= 3x 20-day volume, close above open; buy next open,
   hold 20 sessions, equal weight up to 20 names"""
import sys;sys.path.insert(0,'/home/claude/research')
from math import sqrt
import batch1 as B, harness as H
D,days,close=B.D,B.days,B.close
opens=D['open'];ser=D['series']
def volman(i,target=0.15,cap=2.0):
    if i<21:return {}
    c=[close['SPY'][days[k]] for k in range(i-20,i+1)];r=[c[k+1]/c[k]-1 for k in range(20)]
    m=sum(r)/20;v=sqrt(sum((x-m)**2 for x in r)/20)*sqrt(252)
    return {'SPY':min(cap,target/v)} if v>0 else {}
vol={s:{b['date']:b['volume'] for b in ser[s]} for s in B.STOCKS}
idx={s:{b['date']:k for k,b in enumerate(ser[s])} for s in B.STOCKS}
events={}
for s in B.STOCKS:
    bars=ser[s]
    for k in range(21,len(bars)):
        b=bars[k];av=sum(x['volume'] for x in bars[k-20:k])/20
        if b['open']>=bars[k-1]['close']*1.05 and b['volume']>=3*av and b['close']>b['open']:
            events.setdefault(b['date'],[]).append(s)
pos={}   # symbol -> exit day index
def gapdrift(i,hold=20,maxn=20):
    d=days[i]
    for s in [s for s,e in pos.items() if e<=i]:del pos[s]
    for s in events.get(days[i-1],[]) if i>0 else []:   # signal day i-1 close -> enter: weight applies from close i... 
        pass
    for s in events.get(d,[]):
        if s not in pos and len(pos)<maxn:pos[s]=i+hold
    return {s:1/maxn for s in pos}
if __name__=='__main__':
    H.report('12 vol-managed SPY 15%, cap 2x',D,volman)
    pos.clear();H.report('3p gap-up drift proxy, 20d, 20 slots',D,gapdrift)
