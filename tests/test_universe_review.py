"""The AI universe review: what it is allowed to say, and what gets dropped.

No network - a fake client returns whatever JSON the test wants, so these
assert the sanitising, which is the part that has to hold when the model
misbehaves.
"""

import json

import pytest

from src.config import AnalystConfig, AppConfig, NotionConfig, TossConfig
from src.universe_review import UniverseReviewer, VETO_CATEGORIES

UNIVERSE = ["AAPL", "MSFT", "QQQ"]


class FakeModels:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(contents)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return type("Response", (), {"text": text})()


class FakeClient:
    def __init__(self, payload):
        self.models = FakeModels(payload)


def config_with(**analyst_kwargs):
    return AppConfig(
        toss=TossConfig(client_id="cid", client_secret="sec"),
        notion=NotionConfig(token="t", database_id="d"),
        analyst=AnalystConfig(api_key="k", **analyst_kwargs),
    )


def reviewer_for(payload, **analyst_kwargs):
    return UniverseReviewer(config_with(**analyst_kwargs), client=FakeClient(payload))


def veto_row(symbol="AAPL", category="delisting", reason="r", evidence="headline"):
    return {
        "symbol": symbol,
        "category": category,
        "reason": reason,
        "evidence": evidence,
    }


# ------------------------------------------------------------------- vetoes


def test_a_well_formed_veto_survives():
    review = reviewer_for({"vetoes": [veto_row()], "candidates": []}).review(UNIVERSE)

    assert [v.symbol for v in review.vetoes] == ["AAPL"]
    assert review.vetoes[0].category == "delisting"


def test_a_veto_without_evidence_is_dropped():
    payload = {"vetoes": [veto_row(evidence="  ")], "candidates": []}
    assert reviewer_for(payload).review(UNIVERSE).vetoes == ()


def test_a_veto_with_an_opinion_category_is_dropped():
    # "The valuation looks stretched" is the strategy's business, not a veto.
    payload = {"vetoes": [veto_row(category="overvalued")], "candidates": []}
    assert reviewer_for(payload).review(UNIVERSE).vetoes == ()


def test_a_veto_for_a_symbol_outside_the_universe_is_dropped():
    payload = {"vetoes": [veto_row(symbol="TSLA")], "candidates": []}
    assert reviewer_for(payload).review(UNIVERSE).vetoes == ()


def test_vetoes_are_capped_so_the_model_cannot_halt_everything():
    payload = {
        "vetoes": [veto_row(symbol=s) for s in UNIVERSE],
        "candidates": [],
    }
    review = reviewer_for(payload, max_vetoes=1).review(UNIVERSE)

    assert len(review.vetoes) == 1


def test_duplicate_vetoes_for_one_symbol_count_once():
    payload = {"vetoes": [veto_row(), veto_row()], "candidates": []}
    assert len(reviewer_for(payload).review(UNIVERSE).vetoes) == 1


def test_symbols_are_matched_case_insensitively():
    payload = {"vetoes": [veto_row(symbol="aapl")], "candidates": []}
    assert reviewer_for(payload).review(UNIVERSE).vetoes[0].symbol == "AAPL"


# --------------------------------------------------------------- candidates


def test_a_candidate_already_in_the_universe_is_dropped():
    payload = {"vetoes": [], "candidates": [{"symbol": "MSFT", "name": "n", "reason": "r"}]}
    assert reviewer_for(payload).review(UNIVERSE).candidates == ()


def test_candidates_are_capped():
    rows = [{"symbol": s, "name": s, "reason": "r"} for s in ("A", "B", "C")]
    review = reviewer_for({"vetoes": [], "candidates": rows}, max_candidates=2).review(UNIVERSE)

    assert len(review.candidates) == 2


def test_growth_candidates_are_kept_apart_and_capped():
    rows = [{"symbol": s, "name": s, "reason": "r"} for s in ("ASTS", "OKLO", "RXRX", "JOBY")]
    review = reviewer_for(
        {"vetoes": [], "candidates": [], "growth_candidates": rows}, max_growth_candidates=3
    ).review(UNIVERSE)

    assert [c.symbol for c in review.growth_candidates] == ["ASTS", "OKLO", "RXRX"]
    assert all(c.bucket == "GROWTH" for c in review.growth_candidates)
    assert review.candidates == ()


def test_a_name_in_both_lists_is_shown_once_as_growth():
    row = {"symbol": "OKLO", "name": "Oklo", "reason": "r"}
    review = reviewer_for(
        {"vetoes": [], "candidates": [row], "growth_candidates": [row]}
    ).review(UNIVERSE)

    assert review.candidates == ()
    assert [c.symbol for c in review.growth_candidates] == ["OKLO"]


def test_an_excluded_symbol_is_never_proposed_back():
    # RKLB was taken out of the universe on purpose; a model suggesting it
    # again every morning is noise, whichever list it lands in.
    row = {"symbol": "rklb", "name": "Rocket Lab", "reason": "r"}
    reviewer = reviewer_for(
        {"vetoes": [], "candidates": [row], "growth_candidates": [row]},
        excluded_candidates=("RKLB",),
    )
    review = reviewer.review(UNIVERSE)

    assert review.candidates == () and review.growth_candidates == ()
    assert "RKLB" in reviewer.client.models.calls[0]


def test_a_review_with_only_growth_candidates_is_not_empty():
    row = {"symbol": "OKLO", "name": "Oklo", "reason": "r"}
    review = reviewer_for({"vetoes": [], "candidates": [], "growth_candidates": [row]}).review(
        UNIVERSE
    )
    assert not review.is_empty


# ------------------------------------------------------------------ failure


def test_unparseable_output_reports_an_error_rather_than_an_empty_review():
    review = reviewer_for("not json at all").review(UNIVERSE)

    # "the model said nothing is wrong" and "we could not ask" must not look
    # the same to the caller.
    assert review.error is not None
    assert review.vetoes == ()


def test_review_is_skipped_when_switched_off():
    review = reviewer_for({"vetoes": [], "candidates": []}, universe_review=False).review(UNIVERSE)
    assert review.error is not None


def test_no_client_means_no_review_not_a_crash():
    reviewer = UniverseReviewer(config_with(), client=None)
    reviewer.client = None
    assert reviewer.review(UNIVERSE).error is not None


# ------------------------------------------------------------------- prompt


def test_the_prompt_states_the_allowed_categories_and_the_universe():
    reviewer = reviewer_for({"vetoes": [], "candidates": []})
    reviewer.review(UNIVERSE, holdings=["AAPL"])

    prompt = reviewer.client.models.calls[0]
    for category in VETO_CATEGORIES:
        assert category in prompt
    assert "AAPL, MSFT, QQQ" in prompt
