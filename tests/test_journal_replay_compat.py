"""Release compatibility: a journal written by the deployed r7w code (live 2026-10-15 replay: CRWV round trip,
fee lag, post-close incident, 10-16 settlement) must rebuild to the identical state under the current reducer.
Ledger.rebuild refuses to start on any difference (REBUILD_DIFFERS_FROM_CHECKPOINT), so a reducer change that
alters history (e.g. reading a revised risk policy from code) would stop trading."""
import json

from autonomous_trader.durable.ledger import digest
from autonomous_trader.durable.projection import reduce_event

from journal_r7w_fixture import JOURNAL
R7W_DIGEST = 'c41718a70e52c2538196d796ed8a91d586a64275e95e93fe52dd820dd5bee060'


def test_r7w_journal_replays_identically():
    state = None
    for line in JOURNAL.splitlines():
        state = reduce_event(state, json.loads(line))
    assert digest(state) == R7W_DIGEST
    assert state['policy_hash'] == 'd641fba61515823f230d7204e43602edc79a43f4b86059763368b2c31bcaeaf9'
