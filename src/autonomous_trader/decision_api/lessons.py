"""Lessons ledger for the decision model (owner request 2026-10-15: learn from past mistakes).

Each lesson is something that actually happened on this account, what it taught, and the rule now in
force. Engineering lessons that code already enforces are listed too, so the model does not misread a
result produced by a software fault as a strategy signal. Keep entries short; newest first."""
import json

LESSONS = (
    dict(id='L-004', date='2026-10-19', kind='strategy_evidence',
         what='Lab, 176 liquid names, Oct21-Oct26 (5y), 0.10%/side costs: momentum holds passed every gate: top-20% '
              '60d performers +2.28%/trade, beating SPY by 5% +1.40%, 20d breakouts +1.05%, near-high +1.00%, '
              'positive in both halves, typical hold 10-19 days. Dip-buys lost (-0.04 to -0.17%, 12k trades).',
         rule='Prefer strong names in uptrends (near highs, beating SPY), hold for weeks, exit when the trend breaks '
              '(close under the 50-day). Never buy a dip because it is oversold.'),
    dict(id='L-003', date='2026-10-19', kind='execution',
         what='INTC sold on drift $1.58 above its stop; CRWV hit its stop. Both bought in the first hour; -$15.',
         rule='Drift above the stop is not invalidation. Sales need both models. First-hour entries need the '
              'strongest evidence.'),
    dict(id='L-002', date='2026-10-15', kind='accounting',
         what='A $0.01 broker fee the broker cash had not yet posted halted the next day start.',
         rule='Code now explains fee-only cash differences exactly; nothing for the model to do.'),
    dict(id='L-001', date='2026-10-15', kind='software_fault_not_strategy',
         what='CRWV 3 @66.34 was a good entry; a software incident latch force-sold it 61 minutes later at 67.12 '
              '(+$2.33) before the planned move could develop.',
         rule='A software fault never sells a holding now. Do not treat that exit as evidence about the setup; '
              'a good entry deserves the time its thesis needs.'),
    dict(id='L-000', date='2026-10-14', kind='process',
         what='All-day ABSTAIN on the first live day with a same-day-only scalping playbook.',
         rule='The account is not a day-trading machine: prefer multi-day setups with real upside over '
              'minute-scale spread capture; ABSTAIN only when no setup has positive expected payoff.'),
)


def render(max_chars=2000):   # contracts.Text limit: one supplied block is at most 2000 chars
    rows = list(LESSONS)
    while rows:
        value = json.dumps(dict(kind='lessons_ledger', note='Apply these; newest first.', lessons=rows),
                           separators=(',', ':'))
        if len(value) <= max_chars:
            return value
        rows = rows[:-1]
    return json.dumps(dict(kind='lessons_ledger', lessons=[]))
