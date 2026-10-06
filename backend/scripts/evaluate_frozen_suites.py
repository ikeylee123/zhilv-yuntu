"""Run unchanged frozen evaluators with replay providers and temporary storage."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    from eval.offline_runtime import offline_runtime
    from scripts import evaluate_planning, evaluate_tool_grounded
    datasets={"validator":"planning_eval_cases.json","route":"tool_grounded_eval_cases.json",
        "mcp":"mcp_contract_cases.json","replanning":"replanning_eval_cases.json",
        "selector":"candidate_selection_eval_cases.json","rag_v2":"rag_eval_cases_v2.json"}
    def read(name):return json.loads((ROOT/"eval"/datasets[name]).read_text(encoding="utf-8"))
    with offline_runtime(allow_loopback=True) as runtime:
        from scripts import evaluate_mcp_contract, evaluate_replanning, evaluate_candidate_selection, evaluate_rag_v2
        reports={"validator":evaluate_planning.evaluate(read("validator")["cases"]),
            "route":evaluate_tool_grounded.evaluate(read("route")["cases"]),
            "mcp":evaluate_mcp_contract.evaluate(read("mcp")["cases"]),
            "replanning":evaluate_replanning.evaluate(),"selector":evaluate_candidate_selection.evaluate()}
        rag=evaluate_rag_v2.run(ROOT/"eval"/datasets["rag_v2"])
        # Preserve retrieval metrics; do not convert quality shortfalls into a pass rate.
        reports["rag_v2"]={k:v for k,v in rag.items() if k!="cases"}
        network=len(runtime["network_attempts"])
    metadata={"git_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "harness_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "working_tree_dirty":bool(subprocess.check_output(["git","status","--porcelain"],cwd=ROOT,text=True).strip()),
        "date":datetime.now(timezone.utc).date().isoformat(),"python":platform.python_version(),"mode":"offline","providers":"mock/replay",
        "datasets":{name:{"path":"backend/eval/"+file,"sha256":hashlib.sha256((ROOT/"eval"/file).read_bytes()).hexdigest()} for name,file in datasets.items()}}
    args.output.write_text(json.dumps({"metadata":metadata,"network_attempts":network,"reports":reports},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return int(network>0 or any(r.get("mismatches") for r in reports.values()) or not reports["mcp"]["discovery_ok"])


if __name__=="__main__":raise SystemExit(main())
