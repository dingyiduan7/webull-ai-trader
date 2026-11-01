from autonomous_trader.decision_api import strategy_cards as s

GRADES = {'SUPPORTED', 'PLAUSIBLE', 'FOLKLORE', 'NOT_EXECUTABLE'}


def test_cards_are_graded_unique_and_bounded():
    ids = [c['id'] for c in s.CARDS]
    assert len(ids) == len(set(ids))
    assert all(c['grade'] in GRADES for c in s.CARDS)
    assert all({'idea', 'works', 'fails', 'exit', 'sizing'} <= set(c) for c in s.CARDS)
    assert len(s.render()) <= 7000


def test_cards_never_claim_authority_or_certainty():
    text = s.render().lower()
    assert 'reference only' in text and 'guaranteed' not in text
