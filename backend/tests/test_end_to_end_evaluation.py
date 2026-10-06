"""Counterexamples for the evaluation measurements, not duplicated production rules."""
import json
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evaluate_end_to_end import mutation_projection, grounding_correct


@pytest.mark.parametrize("tamper", ["summary", "constraints", "other_day", "other_activity", "budget"])
def test_projection_detects_unauthorized_changes(tamper):
    from eval.replanning_support import base_plan
    from app.models.replanning import PlanPatch, RemovePatch
    base = base_plan(); base.planning_version = 1; base.state_schema_version = 1
    after = base.model_copy(deep=True)
    after.days[0].spots.pop(1)
    # Removing B legitimately reduces its ten-unit ticket estimate.
    after.estimated_budget -= 10
    after.budget_breakdown.total -= 10
    after.budget_breakdown.tickets -= 10
    patch = PlanPatch(base_plan_version=1,target_day=1,operation=RemovePatch(target_activity_id="activity-B"))
    raw = {"change_type":"remove", "target_activity_id":"activity-B"}
    assert mutation_projection(base,after,raw,patch)[2] == []
    if tamper == "summary": after.summary = "injected"
    elif tamper == "constraints": after.constraints.budget_limit = 99999
    elif tamper == "other_day": after.days[1].theme = "injected"
    elif tamper == "other_activity": after.days[0].spots[0].name = "injected"
    else: after.budget_breakdown.total += 100
    assert mutation_projection(base,after,raw,patch)[2]


def test_fact_metric_rejects_fabricated_values():
    from eval.replanning_support import attach_facts, base_plan
    facts = attach_facts(base_plan()).planning_facts.route_facts
    assert grounding_correct(facts,facts,[])
    bad = facts[0].model_copy(update={"duration_seconds":1})
    assert not grounding_correct([bad],facts,[])


def test_e2e_reproducible_and_bad_labels_fail(tmp_path):
    script = ROOT / "scripts/evaluate_end_to_end.py"
    output = tmp_path / "report.json"
    command = [sys.executable,"-B",str(script),"--output",str(output)]
    first = subprocess.run(command,capture_output=True,text=True,encoding="utf-8")
    assert first.returncode == 0, first.stderr
    before = json.loads(output.read_text(encoding="utf-8"))
    second = subprocess.run(command,capture_output=True,text=True,encoding="utf-8")
    assert second.returncode == 0, second.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == before
    assert before["metrics"]["network_attempts"] == 0
    dataset=json.loads((ROOT/"eval/end_to_end_scenarios.json").read_text(encoding="utf-8"))
    dataset["cases"][0]["expected_decision"]="incorrect_label"
    bad=tmp_path/"bad.json";bad.write_text(json.dumps(dataset),encoding="utf-8")
    failed=subprocess.run(command+["--cases",str(bad)],capture_output=True,text=True,encoding="utf-8")
    assert failed.returncode == 1, failed.stderr
    report=json.loads(output.read_text(encoding="utf-8"))
    assert report["metrics"]["failed"] == 1


def test_offline_runtime_blocks_network():
    import socket
    from eval.offline_runtime import offline_runtime
    with offline_runtime() as runtime, socket.socket() as connection:
        with pytest.raises(RuntimeError,match="forbids network"):
            connection.connect(("127.0.0.1",6379))
        assert len(runtime["network_attempts"]) == 1
