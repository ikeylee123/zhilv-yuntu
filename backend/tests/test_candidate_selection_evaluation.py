import json
import pytest
from scripts.evaluate_candidate_selection import DATASET, ReplayClient, evaluate
from app.agents.candidate_selector import CandidateSelector
from app.models.candidate_selection import CandidateSelectionInput

CASES=json.loads(DATASET.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case",CASES,ids=lambda c:c["id"])
def test_fixed_replay_contract(case):
    data=CandidateSelectionInput.model_validate_json(json.dumps(case["input"]))
    client=ReplayClient(case);result=CandidateSelector(client).select(data)
    assert result.error==case["expected_error"] and client.calls==1
    if result.output:
        assert result.output.candidate_id in {c.candidate_id for c in data.candidates}
        assert result.output.candidate_id in case["acceptable_candidate_ids"]


def test_offline_report_is_not_model_quality():
    report=evaluate()
    assert report["cases"]==36 and report["contract_matches"]==[36,36]
    assert report["mismatches"]==[] and report["median_total_tokens"] is None
    assert report["mode"]=="offline_replay"
    assert report["authorized_id_enforcement"]==[2,2]
    assert report["invalid_schema_enforcement"]==[3,3]
    assert report["selection_failure_mapping"]==[10,10]
