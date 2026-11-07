from copy import deepcopy

import pytest

from app.default_checklist import DEFAULT_CHECKLIST
from app.scoring import ChecklistError, compute, validate_checklist

ALL_YES = {"c1": "yes", "c2": "yes", "c3": "yes", "c4": "20", "c5": "yes", "c6": "10", "c7": "yes", "c8": "no"}


def test_default_checklist_is_valid_and_sums_to_100():
    validate_checklist(DEFAULT_CHECKLIST)
    assert sum(c["weight"] for c in DEFAULT_CHECKLIST["criteria"]) == 100


def test_perfect_dialog_scores_100():
    r = compute(DEFAULT_CHECKLIST, ALL_YES)
    assert r["score"] == "100.00"
    assert r["critical"] is False


def test_scale_is_proportional_to_weight():
    # c4: 15 из 20 → 15 баллов из 20; c6: 7 из 10 → 10.5 из 15. Итог 100 - 5 - 4.5 = 90.5
    r = compute(DEFAULT_CHECKLIST, {**ALL_YES, "c4": "15", "c6": "7"})
    assert r["score"] == "90.50"


def test_na_redistributes_weight():
    # c3 (20) N/A → остаётся 80; c7 «Нет» (5) → 75/80*100 = 93.75
    r = compute(DEFAULT_CHECKLIST, {**ALL_YES, "c3": "na", "c7": "no"})
    assert r["score"] == "93.75"
    assert r["criteria"]["c3"]["share"] is None
    assert r["criteria"]["c7"]["max_share"] == "6.25"


def test_rounding_half_up_two_digits():
    # c2 и c3 N/A → вес 65; c1 «Нет» → 55/65*100 = 84.615… → 84.62
    r = compute(DEFAULT_CHECKLIST, {**ALL_YES, "c2": "na", "c3": "na", "c1": "no"})
    assert r["score"] == "84.62"


def test_critical_flag_is_separate_from_score():
    r = compute(DEFAULT_CHECKLIST, {**ALL_YES, "c8": "yes"})
    assert r["score"] == "100.00"
    assert r["critical"] is True


def test_no_applicable_weighted_criteria_gives_no_score():
    cl = {"criteria": [
        {"id": "a", "title": "A", "type": "yes_no", "weight": 10, "na_allowed": True, "na_condition": "x"},
        {"id": "f", "title": "F", "type": "flag", "weight": 0},
    ]}
    r = compute(cl, {"a": "na", "f": "no"})
    assert r["score"] is None


def test_sections_are_computed():
    r = compute(DEFAULT_CHECKLIST, {**ALL_YES, "c1": "no"})
    assert r["sections"]["start"] == "60.00"
    assert r["sections"]["flow"] == "100.00"


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c["criteria"][0].update(weight=-1), "отрицательным"),
    (lambda c: c["criteria"][1].update(na_condition=""), "условие"),
    (lambda c: c["criteria"][3].update(scale=[{"value": 5, "label": "x"}]), "две ступени"),
    (lambda c: c["criteria"][7].update(weight=5), "нет веса"),
    (lambda c: c["criteria"][1].update(id="c1"), "Повторяется"),
])
def test_invalid_checklists_are_rejected(mutate, message):
    cl = deepcopy(DEFAULT_CHECKLIST)
    mutate(cl)
    with pytest.raises(ChecklistError, match=message):
        validate_checklist(cl)
