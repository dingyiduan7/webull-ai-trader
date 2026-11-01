import sys;sys.path[:0]=['/home/claude/research','/home/claude/canon/scripts','/home/claude/canon/src']
import batch1 as B, harness as H
from strategy_lab import UNIVERSE
u=list(UNIVERSE);core=u[:u.index('CRWV')];core=[s for s in core if s in B.STOCKS]
hype=set('CRWV IONQ RGTI SOUN MSTR MARA RIOT CLSK APP RDDT PLTR HOOD COIN SMCI ARM CRWD'.split())
for name,pool in (('core large caps (%d)'%len(core),core),('core minus hype names',[s for s in core if s not in hype]),('affordable mid/small list',[s for s in u[u.index('SNAP'):] if s in B.STOCKS])):
    B.STOCKS[:]=pool
    H.report('7 diag: '+name,B.D,B.monthly(B.mom_12_1))
