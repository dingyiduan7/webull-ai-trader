"""Idea 1 with real fund prices (pre-registered): hold the leveraged fund while its underlying index ETF closes
above its 200-day average, otherwise BIL (T-bills). Development window only; signal at close, applied next day."""
import sys;sys.path.insert(0,'/home/claude/research')
import harness as H
D=H.load('/mnt/user-data/uploads/auto trader/.local/research/etf_bars.json')
days,close=D['days'],D['close']
days[:]=[d for d in days if all(d in close[s] for s in ('SPY','QQQ','SSO','UPRO','QLD','TQQQ','BIL'))]
H.DEV=(days[253],H.DEV[1]);H.MID=days[(253+max(i for i,d in enumerate(days) if d<=H.DEV[1]))//2]
print('window',H.DEV,'split',H.MID)
def trend(fund,base,n=200):
    def w(i):
        if i<n:return {'BIL':1.0}
        c=[close[base][days[k]] for k in range(i-n+1,i+1)]
        return {fund:1.0} if c[-1]>sum(c)/n else {'BIL':1.0}
    return w
hold=lambda f:(lambda i:{f:1.0})
for f,b in (('SSO','SPY'),('UPRO','SPY'),('QLD','QQQ'),('TQQQ','QQQ')):
    H.report('1  %s while %s > 200-day, else BIL'%(f,b),D,trend(f,b))
    H.report('   %s buy and hold (reference)'%f,D,hold(f))
H.report('   QQQ buy and hold (reference)',D,hold('QQQ'))
