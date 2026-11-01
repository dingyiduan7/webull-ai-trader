"""Batch 3 (pre-registered 2026-10-22), ETFs only (no survivorship problem):
13 Connors RSI-2 on SPY/QQQ/IWM/DIA: buy when RSI2<10 and close > 200-day, exit when close > 5-day; equal split
14 dual momentum: hold SPY if its 12-month return beats both cash (3%) and TLT, else TLT if TLT beats cash, else cash; monthly"""
import sys;sys.path.insert(0,'/home/claude/research')
import batch1 as B, harness as H
from autonomous_trader_lab_shim import rsi  # noqa
D,days,close=B.D,B.days,B.close
ETF4=['SPY','QQQ','IWM','DIA']
state={s:False for s in ETF4}
def connors(i):
    w={}
    for s in ETF4:
        c=[close[s][days[k]] for k in range(max(0,i-200),i+1)]
        if len(c)<201:continue
        if state[s]:
            if c[-1]>sum(c[-5:])/5:state[s]=False
        else:
            if rsi(c,2,len(c)-1)<10 and c[-1]>sum(c[-200:])/200:state[s]=True
        if state[s]:w[s]=1/len(ETF4)
    return w
def dual(i):
    if i<253:return {}
    r=lambda s:close[s][days[i]]/close[s][days[i-252]]-1
    if r('SPY')>max(0.03,r('TLT')):return {'SPY':1.0}
    if r('TLT')>0.03:return {'TLT':1.0}
    return {}
if __name__=='__main__':
    H.report('13 Connors RSI-2 on 4 index ETFs',D,connors)
    H.report('14 dual momentum SPY/TLT/cash monthly',D,B.monthly(dual))
