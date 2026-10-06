"""Portable offline pytest runner with collected-function/instance inventory."""
import argparse
import hashlib
import subprocess
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

SUITES = {
    "3.5": ["test_rag_destination_metadata", "test_rag_retriever", "test_rag_tool", "test_rag_eval_cases", "test_rag_hardening", "test_rag_index", "test_rag_evaluation_v2", "test_rag_integrity_hardening"],
    "3C": ["test_candidate_selector", "test_candidate_selector_hardening", "test_candidate_selection_evaluation", "test_replanning_llm_selection"],
    "3B": ["test_replanning_scope", "test_plan_patch_service", "test_replanning_candidates", "test_replanning_facts", "test_replanning_preservation", "test_replanning_commit", "test_replanning_tool_efficiency", "test_replanning_evaluation", "test_replanning_correctness_hardening"],
    "3A": ["test_authoritative_state", "test_state_boundary_hardening"],
    "2.5": ["test_tool_contracts", "test_tool_adapter", "test_mcp_gateway", "test_amap_secret_logging"],
    "2": ["test_route_facts", "test_route_workflow", "test_route_hardening", "test_planning_fact_service", "test_tool_grounded_evaluation"],
    "1": ["test_plan_validator", "test_planning_hardening", "test_planning_workflow", "test_planning_evaluation"],
    "4": ["test_end_to_end_evaluation"],
}


class Inventory:
    def __init__(self):
        self.items=[];self.outcomes=Counter();self.errors=[];self.per_file=defaultdict(lambda:{"functions":set(),"expanded_instances":0})
    def pytest_collection_finish(self, session):
        self.items=list(session.items)
        for item in self.items:
            name=Path(str(item.path)).name
            self.per_file[name]["functions"].add(item.nodeid.split("[")[0])
            self.per_file[name]["expanded_instances"]+=1
    def pytest_collectreport(self,report):
        if report.failed:self.errors.append(report.nodeid)
    def pytest_runtest_logreport(self,report):
        if report.when=="call" or report.failed or report.skipped:
            self.outcomes[report.outcome]+=1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite",choices=["full",*SUITES],default="full")
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    from eval.offline_runtime import offline_runtime
    import pytest
    recorder=Inventory()
    with offline_runtime(allow_loopback=True) as runtime:
        paths=[str(ROOT/"tests")] if args.suite=="full" else [str(ROOT/"tests"/(n+".py")) for n in SUITES[args.suite]]
        code=pytest.main(paths+["-q","-p","no:cacheprovider","--basetemp",str(Path(runtime["folder"])/"pytest")],plugins=[recorder])
        result={"metadata":{"git_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
            "date":datetime.now(timezone.utc).date().isoformat(), "runner_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "test_source_sha256":{name:hashlib.sha256((ROOT/"tests"/name).read_bytes()).hexdigest() for name in sorted(recorder.per_file)}},
            "suite":args.suite,"python":platform.python_version(),"mode":"offline","providers":"mock/replay",
            "passed":recorder.outcomes["passed"],"failed":recorder.outcomes["failed"],"skipped":recorder.outcomes["skipped"],
            "collection_errors":recorder.errors,"network_attempts":len(runtime["network_attempts"]),
            "test_functions":sum(len(v["functions"]) for v in recorder.per_file.values()),"expanded_instances":len(recorder.items),
            "files":{name:{"test_functions":len(v["functions"]),"expanded_instances":v["expanded_instances"]} for name,v in sorted(recorder.per_file.items())}}
    if args.output:args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    return int(code)


if __name__=="__main__":raise SystemExit(main())
