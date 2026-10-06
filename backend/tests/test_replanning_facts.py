import pytest
from test_authoritative_state import database
from eval.replanning_support import *
from app.models.replanning import PlanChangeRequest
from app.services.replanning_scope import route_queries

@pytest.mark.parametrize("kind,kwargs,invalid,reused,collected",[
    ("replace",{},2,2,2),("remove",{},2,2,1),("add",{},1,3,2),
    ("modify_time",{},0,4,0),("modify_time",dict(start_time="17:00",end_time="18:00"),2,2,2)])
def test_query_diff(database,kind,kwargs,invalid,reused,collected):
    state=create();svc=service();raw=change(kind,**kwargs)
    if kind in ("replace","add"):raw=grant(svc,raw)
    result=svc.replan(PlanChangeRequest(**raw));assert result.decision=="accepted",result
    scope=result.scope
    assert (len(scope.facts_to_invalidate),len(scope.reused_queries),len(scope.facts_to_recollect))==(invalid,reused,collected)
    old=state.itinerary.planning_facts
    facts=result.itinerary.planning_facts
    assert facts.metadata.plan_fingerprint!=old.metadata.plan_fingerprint
    for f in facts.route_facts:
        key=build_tool_query_key("get_driving_route",dict(origin=f.origin.coordinates.model_dump(),destination=f.destination.coordinates.model_dump()))
        if key in scope.reused_queries:
            original=next(a for a in old.route_facts if a.origin.entity_id==f.origin.entity_id and a.destination.entity_id==f.destination.entity_id)
            assert (f.source,f.distance_meters,f.duration_seconds)==(original.source,original.distance_meters,original.duration_seconds)


def test_stale_unrelated_recollected(database):
    create(attach_facts(base_plan(),stale=("E>F",)));svc=service()
    result=svc.replan(PlanChangeRequest(**change()))
    assert result.decision=="accepted"
    assert len(result.scope.facts_to_recollect)==2


def test_wrong_snapshot_not_blindly_rebound(database):
    create(attach_facts(base_plan(),bad_fingerprint=True));svc=service()
    result=svc.replan(PlanChangeRequest(**change()))
    assert result.decision=="unverifiable" and result.scope.reused_queries==()
    assert svc.provider.calls==[]


def test_missing_observation_with_bound_endpoints(database):
    create(attach_facts(base_plan(),missing=("B>C",)));svc=service()
    result=svc.replan(PlanChangeRequest(**change("modify_time")))
    assert result.decision=="accepted" and len(result.scope.facts_to_recollect)==1


def test_route_disabled_does_not_silently_drop_mode_requirement(database):
    plan=base_plan();plan.constraints.require_route_feasibility=False
    create(attach_facts(plan));svc=service()
    result=svc.replan(PlanChangeRequest(**change()))
    assert result.decision=="unverifiable" and svc.provider.calls==[]


def test_conflicting_snapshot_observations_not_reused(database):
    plan=attach_facts(base_plan())
    old=plan.planning_facts
    bad=old.route_facts[-1].model_copy(update={"duration_seconds":42})
    plan.planning_facts=old.model_copy(update={"route_facts":old.route_facts+(bad,)})
    create(plan);svc=service()
    result=svc.replan(PlanChangeRequest(**change()))
    assert result.decision=="unverifiable"
    assert len(result.scope.reused_queries)==1


def test_reuse_expiring_during_collection_prevents_commit(database):
    from datetime import timedelta
    from test_replanning_commit import row
    create();svc=service();before=row();ticks=[NOW]
    svc.clock=lambda:ticks[0]
    svc.provider.hook=lambda name,args:ticks.__setitem__(0,NOW+timedelta(seconds=950))
    result=svc.replan(PlanChangeRequest(**change()))
    assert result.decision=="unverifiable" and row()==before
