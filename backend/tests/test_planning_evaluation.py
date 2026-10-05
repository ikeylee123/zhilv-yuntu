"""Fixed dataset expectations and evaluator integrity, not production accuracy claims."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("planning_evaluator", ROOT / "scripts" / "evaluate_planning.py")
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)
CASES = json.loads((ROOT / "eval" / "planning_eval_cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_fixed_synthetic_case(case):
    actual = evaluator.predict(case["input"])
    assert actual["status"] == case["expected"]["status"]
    assert set(actual["error_types"]) == set(case["expected"]["error_types"])
    assert set(actual["unknown_types"]) == set(case["expected"]["unknown_types"])


def test_predictions_are_independent_of_labels_and_case_ids():
    case = deepcopy(CASES[0])
    expected_prediction = evaluator.predict(case["input"])
    case["id"] = "malicious-label"
    case["expected"] = {"status": "invalid", "error_types": ["invented"], "unknown_types": []}
    assert evaluator.predict(case["input"]) == expected_prediction
    results = evaluator.evaluate([case])
    assert results["overall_validation_accuracy"] == 0
    assert results["fnr"] == 1
    assert results["mismatches"][0]["id"] == "malicious-label"


def test_metric_denominators_and_false_positives():
    case = deepcopy(next(c for c in CASES if c["category"] == "budget"))
    case["expected"] = {"status": "valid", "error_types": [], "unknown_types": []}
    results = evaluator.evaluate([case])
    assert results["confusion"] == {"tp": 0, "tn": 0, "fp": 1, "fn": 0}
    assert results["fpr"] == 1 and results["recall"] is None
    assert results["unknown_detection_rate"] is None
    assert results["per_violation_type"]["budget_exceeded"]["fp"] == 1


def test_dataset_size_and_mixed_unknown_semantics():
    assert len(CASES) == 67
    assert len({case["id"] for case in CASES}) == len(CASES)
    result = evaluator.evaluate(CASES)
    assert result["confusion"] == {"tp": 52, "tn": 15, "fp": 0, "fn": 0}
    assert result["unknown_detection_rate"] == 1
    mixed = next(c for c in CASES if c["category"] == "mixed")
    assert evaluator.predict(mixed["input"])["status"] == "invalid"
