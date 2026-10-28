"""The evaluation dataset and scorer (the evaluation itself calls the real API and is not run here)."""

import json
from collections import Counter

from app.schemas import Action, ExtractedAction
from scripts.eval_extraction import PHRASES, compare
from scripts.eval_voice import similarity

phrases = json.loads(PHRASES.read_text(encoding="utf-8"))


def test_dataset_size_and_languages():
    assert len(phrases) == 33
    assert len({p["id"] for p in phrases}) == 33
    assert Counter(p["language"] for p in phrases) == {"fr": 13, "en": 10, "pcm": 10}


def test_expected_actions_are_valid_extractions():
    for phrase in phrases:
        for action in phrase["expected"]:
            ExtractedAction(**action)


def test_dataset_covers_every_case():
    actions = Counter(a["action"] for p in phrases for a in p["expected"])
    assert set(actions) == {a.value for a in Action}
    assert any(len(p["expected"]) > 1 for p in phrases)
    assert any("total_price" in a and a["action"] != "debt_payment" for p in phrases for a in p["expected"])
    assert any("price" in a for p in phrases for a in p["expected"])


def test_compare_exact_match():
    expected = [{"action": "sale", "product": "savon", "quantity": 3, "price": 250}]
    got = [{"action": "sale", "product": "Savons", "quantity": 3.0, "unit": None, "price": 250, "total_price": None, "customer": None}]
    assert compare(expected, got) == []


def test_compare_reports_differences():
    expected = [{"action": "sale", "product": "riz", "quantity": 3, "total_price": 25000}]
    got = [{"action": "sale", "product": "riz", "quantity": 3, "price": 8333, "total_price": 25000}]  # computed price
    assert compare(expected, got) == ["#1 price 8333 != None"]
    assert compare(expected, []) == ["0 action(s) instead of 1"]


def test_compare_quantity_one_accepts_price_or_total():
    expected = [{"action": "credit_sale", "product": "rice", "quantity": 1, "total_price": 23000, "customer": "Mama Ngozi"}]
    got = [{"action": "credit_sale", "product": "bag of rice", "quantity": 1, "price": 23000, "customer": "Mama Ngozi"}]
    assert compare(expected, got) == []


def test_voice_similarity():
    assert similarity("J'ai vendu 3 sacs de riz pour 25 000", "jai vendu 3 sacs de riz pour 25000.") == 1
    assert similarity("vendu 3 savons", "vendu trois savons") < 1
    assert similarity("vendu 3 savons", "") == 0
