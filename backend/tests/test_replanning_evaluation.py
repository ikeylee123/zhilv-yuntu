import json
from pathlib import Path
import pytest
from test_authoritative_state import database
from scripts.evaluate_replanning import run_case
CASES=json.loads((Path(__file__).resolve().parents[1]/"eval/replanning_eval_cases.json").read_text())

@pytest.mark.parametrize("case",CASES,ids=[c["id"] for c in CASES])
def test_offline_case(database,case):
    checks,actual=run_case(case)
    assert all(checks.values()),(case["id"],checks,actual)
