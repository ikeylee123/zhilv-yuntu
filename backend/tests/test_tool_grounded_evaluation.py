from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("tool_grounded_eval", ROOT / "scripts/evaluate_tool_grounded.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)
CASES = json.loads((ROOT / "eval/tool_grounded_eval_cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_fixed_route_evidence_fixture(case):
    prediction = evaluation.predict(case["input"])
    assert prediction == {key: case["expected"][key] for key in prediction}


def test_predictions_do_not_receive_labels_or_ids(monkeypatch):
    observed = []
    original = evaluation.predict
    def spy(inputs):
        assert "expected" not in inputs and "id" not in inputs and "category" not in inputs
        observed.append(inputs)
        return original(inputs)
    monkeypatch.setattr(evaluation, "predict", spy)
    altered = deepcopy(CASES[:1])
    altered[0]["id"] = "not-a-special-case"
    altered[0]["expected"]["status"] = "invalid"
    result = evaluation.evaluate(altered)
    assert len(observed) == 1
    assert result["three_state_status_accuracy"] == 0
    assert result["mismatches"]


def test_zero_denominators_are_na():
    result = evaluation.evaluate([])
    assert result["three_state_status_accuracy"] is None
    assert result["known_fact_utilization_rate"] is None
    assert result["route_verified_fraction"] is None
    assert result["unknown_detection_rate"] is None
    case = deepcopy(next(c for c in CASES if c["id"] == "combined-zero-not-required"))
    result = evaluation.evaluate([case])
    assert result["three_state_status_accuracy"] == 1
    assert result["route_verified_fraction"] is None
    assert result["known_fact_utilization_rate"] is None


def test_metrics_expose_missed_violations(monkeypatch):
    case = deepcopy(next(c for c in CASES if c["id"] == "route-insufficient"))
    actual = evaluation.predict(case["input"])
    actual.update(status="valid", error_types=[])
    monkeypatch.setattr(evaluation, "predict", lambda inputs: actual)
    result = evaluation.evaluate([case])
    assert result["confusion"]["fn"] == 1
    assert result["route_time_violation_detection_rate"] == 0
    assert result["violation_recall"] == 0


def test_dataset_has_declared_categories_and_fixed_count():
    from collections import Counter
    assert len(CASES) == len({c["id"] for c in CASES}) == 48
    assert Counter(c["category"] for c in CASES) == {"route": 16, "identity": 8, "freshness": 8, "failure": 8, "combined": 8}
    assert evaluation.evaluate(CASES)["mismatches"] == []
