"""A7 vocabulary gate — the deterministic compliance check."""

from el.domain.vocabulary import is_compliant, vocabulary_violations


def test_clean_fit_language_passes():
    text = (
        "This market is a weak proxy: it captures consumer usage, not "
        "enterprise adoption. The best available expression has high "
        "resolution risk. No clean expression exists for the mechanism."
    )
    assert is_compliant(text)


def test_restricted_phrases_caught():
    assert vocabulary_violations("a guaranteed edge, basically risk-free")
    assert vocabulary_violations("you should trade this now")


def test_trading_verbs_caught_as_words():
    assert vocabulary_violations("just buy the YES side") == ["buy"]
    assert vocabulary_violations("Sell it before resolution") == ["sell"]


def test_no_false_hits_inside_words():
    # 'buyer'/'sellers'/'resell' must not trip the word check
    assert is_compliant("buyers and sellers set the odds; resellers exist")
