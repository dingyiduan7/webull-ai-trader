import sys;sys.path[:0]=['/home/claude/research','/home/claude/canon/scripts']
import batch2 as G, harness as H
from strategy_lab import UNIVERSE
u=list(UNIVERSE);core=set(u[:u.index('CRWV')])-set('CRWV IONQ RGTI SOUN MSTR MARA RIOT CLSK APP RDDT PLTR HOOD COIN SMCI ARM CRWD'.split())
allev=dict(G.events)
G.events.clear();G.events.update({d:[s for s in v if s in core] for d,v in allev.items()})
G.pos.clear();H.report('3p diag: core minus hype',G.D,G.gapdrift)
G.events.clear();G.events.update({d:[s for s in v if s not in core] for d,v in allev.items()})
G.pos.clear();H.report('3p diag: mid/small + hype only',G.D,G.gapdrift)
