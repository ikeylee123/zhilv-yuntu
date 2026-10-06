"""Deterministic fixture evaluation; expected labels are external, never generated here."""
import json
import os
from pathlib import Path
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def run_case(case):
    from eval.replanning_support import (FakeProvider, service, create, attach_facts, poi, NOW)
    from app.models.schemas import Itinerary, TripConstraints
    from app.models.replanning import PlanChangeRequest, StateError
    from app.models.db_models import TripRecord
    from app.services import storage_service as store, replanning_service as orchestration
    from app.services.authoritative_state import compare_and_swap_plan
    from app.models.tool_contracts import build_tool_query_key
    from pydantic import ValidationError
    from unittest.mock import patch
    def row():
        with store.SessionLocal() as db:
            r=db.query(TripRecord).filter_by(trip_id="fixture").one()
            return {c.name:getattr(r,c.name) for c in TripRecord.__table__.columns}
    with store.SessionLocal() as db:
        db.query(TripRecord).delete(); db.commit()
    plan=Itinerary.model_validate(case["base_plan"])
    plan.constraints=TripConstraints.model_validate(case["constraints"])
    attach_facts(plan,**case["facts"])
    state=create(plan);before=row();observed=[]
    assert state.planning_version==case["base_version"]
    provider=FakeProvider(items=[poi(n) for n in case["allowed_candidates"]],timeout=case.get("timeout",False))
    svc=service(provider);svc.clock=lambda:datetime.fromisoformat(case["evaluation_time"])
    raw=dict(case["change"])
    # Server-only fixture grants isolate authorization failures from candidate search filtering.
    from app.models.replanning import AllowedCandidateSet, GroundedCandidate
    if raw.get("change_type") in ("replace","add"):
        svc.store.put(AllowedCandidateSet(candidate_set_id="grant",trip_id=case.get("grant_trip","fixture"),
            base_plan_version=1,target_day=raw["target_day_index"],target_activity_id=raw.get("target_activity_id"),
            operation=raw["change_type"],created_at=NOW-timedelta(seconds=case.get("grant_age",0)),fetched_at=NOW,
            candidates=tuple(GroundedCandidate(poi=poi(n),estimated_cost=30) for n in case["allowed_candidates"])))
    winner=[]
    if case.get("race"):
        def hook(name,args):
            if name=="get_driving_route" and not winner:
                p=state.itinerary.model_copy(deep=True);p.summary="winner"
                compare_and_swap_plan("fixture",1,p,evaluation_time=NOW);winner.append(row())
        provider.hook=hook
    original=orchestration.merge_patch
    def capture(*args):
        candidate=original(*args)
        observed.append(candidate)
        if case.get("tamper"): candidate.days[1].theme="unauthorized"
        return candidate
    try:
        request=PlanChangeRequest(**raw)
        with patch.object(orchestration,"merge_patch",capture):result=svc.replan(request)
        scope=result.scope
        decision=result.decision
        status=result.validation_report.status if result.validation_report else None
        unauthorized=result.reason=="UNAUTHORIZED_PLAN_MUTATION"
    except ValidationError:
        result=scope=None;decision="rejected";status=None;unauthorized=False
    after=row()
    def keys(labels):
        answer=[]
        for label in labels:
            a,b=label.split(">")
            answer.append(build_tool_query_key("get_driving_route",dict(origin=poi(a).coordinates.model_dump(),destination=poi(b).coordinates.model_dump())))
        return sorted(answer)
    expected_scope=case["expected_scope"]
    actual_scope=None if scope is None else dict(day=scope.affected_day,operation=scope.allowed_operation.value,affected=list(scope.affected_activity_ids))
    changed=[]
    preserved=True
    if observed:
        old={s.activity_id:s for d in state.itinerary.days for s in d.spots}
        new={s.activity_id:s for d in observed[0].days for s in d.spots}
        changed=sorted(k for k in set(old)|set(new) if old.get(k)!=new.get(k))
        preserved=all(old[k]==new.get(k) for k in case["expected_preserved_activity_ids"])
        preserved=preserved and all(a==b for a,b in zip(state.itinerary.days,observed[0].days) if a.day_index!=raw["target_day_index"])
    scope_preserved=list(scope.preserved_activity_ids) if scope else []
    checks={
        "Scope accuracy":actual_scope==expected_scope and scope_preserved==case["expected_preserved_activity_ids"],
        "Preserved projection accuracy":preserved if not case.get("tamper") else unauthorized,
        "Merge correctness":changed==sorted(case["expected_changed_activity_ids"]),
        "Fact invalidation accuracy":sorted(scope.facts_to_invalidate if scope else ())==keys(case["expected_invalidated_queries"]),
        "Fact reuse accuracy":sorted(scope.reused_queries if scope else ())==keys(case["expected_reused_queries"]),
        "Fact recollection accuracy":sorted(scope.facts_to_recollect if scope else ())==keys(case["expected_recollected_queries"]),
        "Three-state validation accuracy":status==case["expected_status"],
        "Decision accuracy":decision==case["expected_decision"],
        "Active-plan change accuracy":(after!=before)==case["expected_active_plan_changed"],
    }
    if decision!="accepted":checks["Active-plan preservation on failure"]=after==(winner[0] if winner else before)
    if case.get("tamper"):checks["Unauthorized mutation detection"]=unauthorized
    if case["expected_decision"]=="conflict":checks["Version conflict rejection"]=decision=="conflict"
    return checks,dict(decision=decision,status=status,reason=result.reason if result else "INVALID_CHANGE_REQUEST")


def evaluate():
    cases=json.loads((ROOT/"eval/replanning_eval_cases.json").read_text(encoding="utf-8"))
    metrics={};mismatches=[]
    for case in cases:
        checks,actual=run_case(case)
        for name,passed in checks.items():
            totals=metrics.setdefault(name,[0,0]);totals[0]+=int(passed);totals[1]+=1
        failed=[name for name,passed in checks.items() if not passed]
        if failed:mismatches.append(dict(id=case["id"],failed=failed,actual=actual))
    return dict(cases=len(cases),distribution=dict(Counter(c["category"] for c in cases)),metrics=metrics,mismatches=mismatches)


def main():
    # Configure an isolated temporary database before importing any storage-bound module.
    os.environ["AMAP_API_KEY"]="";os.environ["LLM_API_KEY"]="";os.environ["REDIS_ENABLED"]="false"
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.config as config
    with tempfile.TemporaryDirectory(prefix="replanning-eval-") as folder:
        engine=create_engine("sqlite:///"+str(Path(folder)/"eval.db"))
        config.engine=engine;config.SessionLocal=sessionmaker(bind=engine)
        from app.services import storage_service as store
        store.engine=engine;store.SessionLocal=config.SessionLocal;store.init_db()
        result=evaluate();engine.dispose()
    print(json.dumps(result,indent=2))
    return bool(result["mismatches"])


if __name__=="__main__":raise SystemExit(main())
