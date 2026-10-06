"""Adversarial cases absent from the original transport-free evaluation fixtures."""
from decimal import Decimal
from datetime import timedelta
import json
import pytest
from test_authoritative_state import database
from test_replanning_commit import row
from eval.replanning_support import *
from app.models.replanning import PlanChangeRequest, CandidatePreparationRequest, StateError
from app.models.schemas import TransportItem, HotelItem, PlanningState, PlaceReference
from app.services import replanning_transport as transport_policy, plan_patch_service as patches
from app.services.replanning_scope import authorize_scope
from app.services.plan_validator import validate_plan


def transport_plan(*, names=("A","B","B","C"), costs=(50,50), hotel=None):
    plan=base_plan()
    plan.days[0].transport=[TransportItem(mode="driving",from_place=names[0],to_place=names[1],estimated_cost=costs[0]),
        TransportItem(mode="driving",from_place=names[2],to_place=names[3],estimated_cost=costs[1])]
    plan.days[0].hotel=HotelItem(name=hotel,estimated_cost=0) if hotel else None
    plan.budget_breakdown.transport=float(sum(costs))
    plan.budget_breakdown.total=plan.estimated_budget=float(60+sum(costs))
    return plan


@pytest.mark.parametrize("hotel",["B"," b ","b"])
def test_hotel_transfer_ambiguity_preserves_entire_row(database,hotel):
    plan=transport_plan(hotel=hotel)
    plan.days[0].transport=[TransportItem(mode="driving",from_place="Airport",to_place="B",estimated_cost=100)]
    create(attach_facts(plan));before=row();svc=service()
    result=svc.replan(PlanChangeRequest(**change()))
    assert (result.decision,result.reason)==("unverifiable","TRANSPORT_BINDING_UNVERIFIABLE")
    assert row()==before and svc.provider.calls==[]


@pytest.mark.parametrize("names",[("A","B","B","C"),(" a "," b ","b"," c ")])
def test_unique_attraction_binding_and_normalization(database,names):
    create(attach_facts(transport_plan(names=names,hotel="H")));svc=service()
    result=svc.replan(PlanChangeRequest(**change()))
    assert result.decision=="accepted",result
    assert [(t.from_place,t.to_place) for t in result.itinerary.days[0].transport]==[("A","C")]
    assert result.itinerary.estimated_budget==62.2
    assert result.change_summary["new_transport_cost_policy"]=="existing-route-distance-heuristic"


def test_duplicate_normalized_attraction_is_ambiguous(database):
    plan=transport_plan();plan.days[0].spots[2].name=" b "
    create(attach_facts(plan));before=row();result=service().replan(PlanChangeRequest(**change()))
    assert result.reason=="TRANSPORT_BINDING_UNVERIFIABLE" and row()==before


def test_target_endpoint_with_unbound_peer_fails_closed(database):
    plan=transport_plan();plan.days[0].transport[0].from_place="unknown departure"
    create(attach_facts(plan));before=row();result=service().replan(PlanChangeRequest(**change()))
    assert result.reason=="TRANSPORT_BINDING_UNVERIFIABLE" and row()==before


def test_unrelated_airport_hotel_transfer_exactly_preserved(database):
    plan=base_plan();plan.days[0].hotel=HotelItem(name="H",estimated_cost=0)
    plan.days[0].transport=[TransportItem(mode="driving",from_place="Airport",to_place="H",estimated_cost=70,duration="old")]
    plan.estimated_budget=plan.budget_breakdown.total=130.0;plan.budget_breakdown.transport=70.0
    state=create(attach_facts(plan));result=service().replan(PlanChangeRequest(**change()))
    assert result.decision=="accepted"
    assert result.itinerary.days[0].transport[0]==state.itinerary.days[0].transport[0]


def test_preservation_does_not_repeat_mutation_helper(database,monkeypatch):
    plan=base_plan();plan.days[0].transport=[TransportItem(mode="driving",from_place="Airport",to_place="H",estimated_cost=0)]
    state=create(attach_facts(plan));request=PlanChangeRequest(**change())
    patch=patches.build_patch(state,request);candidate=patches.merge_patch(state,authorize_scope(state,request),patch)
    candidate.days[0].transport=[]
    monkeypatch.setattr(patches,"_derived",lambda *a,**kw:None)
    with pytest.raises(StateError,match="UNAUTHORIZED_PLAN_MUTATION"):
        patches.assert_preserved_projection(state.itinerary,candidate,patch)


def test_corrupt_mutator_cannot_remove_unrelated_transfer(database,monkeypatch):
    plan=base_plan();plan.days[0].transport=[TransportItem(mode="driving",from_place="Airport",to_place="H",estimated_cost=0)]
    create(attach_facts(plan));before=row();real=patches._derived
    def bad(base,candidate,patch,**kw):
        policy=real(base,candidate,patch,**kw);candidate.days[0].transport=[];return policy
    monkeypatch.setattr(patches,"_derived",bad)
    result=service().replan(PlanChangeRequest(**change()))
    assert result.reason=="UNAUTHORIZED_PLAN_MUTATION" and row()==before


@pytest.mark.parametrize("kind",["remove","add","replace","modify_time"])
def test_unknown_new_cost_is_not_zero(database,monkeypatch,kind):
    create(attach_facts(transport_plan()));before=row();svc=service()
    monkeypatch.setattr(transport_policy,"estimate_transport",lambda *args:None)
    raw=change(kind)
    if kind=="modify_time":raw.update(start_time="17:00",end_time="18:00")
    if kind in ("add","replace"):raw=grant(svc,raw)
    result=svc.replan(PlanChangeRequest(**raw))
    assert result.decision=="unverifiable",result
    assert any(v.type=="budget_unverifiable" for v in result.validation_report.unknowns)
    assert result.change_summary["transport_cost_status"]=="unknown" and row()==before


def test_exact_bridge_counterexample_unknown_vs_known_estimate(database,monkeypatch):
    plan=transport_plan();plan.constraints.budget_limit=Decimal(100)
    create(attach_facts(plan));before=row()
    monkeypatch.setattr(transport_policy,"estimate_transport",lambda *args:None)
    result=service().replan(PlanChangeRequest(**change()))
    assert result.decision=="unverifiable" and row()==before
    assert result.validation_report.route_coverage.verified_pairs==3


@pytest.mark.parametrize("cost",[0,25])
def test_explicit_applicable_existing_cost_including_zero(database,monkeypatch,cost):
    plan=base_plan();plan.days[0].transport=[TransportItem(mode="driving",from_place="A",to_place="C",estimated_cost=cost)]
    plan.estimated_budget=plan.budget_breakdown.total=float(60+cost);plan.budget_breakdown.transport=float(cost)
    create(attach_facts(plan));monkeypatch.setattr(transport_policy,"estimate_transport",lambda *a:None)
    result=service().replan(PlanChangeRequest(**change()))
    assert result.decision=="accepted" and result.itinerary.estimated_budget==50+cost
    assert len(result.itinerary.days[0].transport)==1


def test_historical_zero_has_no_reconstructed_provenance(database):
    plan=base_plan();raw=plan.model_dump();raw["days"][0]["spots"][1].pop("estimated_cost")
    state=create(attach_facts(Itinerary.model_validate(raw)))
    assert state.itinerary.days[0].spots[1].estimated_cost==0
    assert "cost_provenance" not in state.itinerary.days[0].spots[1].model_dump()


def test_hard_budget_violation_outranks_unknown_cost(database,monkeypatch):
    plan=transport_plan();plan.constraints.budget_limit=Decimal(40)
    create(attach_facts(plan));before=row();monkeypatch.setattr(transport_policy,"estimate_transport",lambda *a:None)
    result=service().replan(PlanChangeRequest(**change()))
    assert result.decision=="rejected" and result.validation_report.status=="invalid" and row()==before
    assert any(v.type=="budget_exceeded" for v in result.validation_report.violations)
    assert any(v.type=="budget_unverifiable" for v in result.validation_report.unknowns)


@pytest.mark.parametrize("changes",[dict(category=None),dict(category="spot"),dict(adcode=None),dict(adcode="330102")])
def test_detail_missing_or_agreeing_context_retains_search(database,changes):
    create();svc=service();raw=grant(svc,change("replace"));svc.provider.items=(poi("X").model_copy(update=changes),)
    result=svc.replan(PlanChangeRequest(**raw));assert result.decision=="accepted"


@pytest.mark.parametrize("changes",[dict(category="hotel"),dict(adcode="110101"),dict(name="other"),dict(coordinates=poi("Y").coordinates),dict(poi_id="Y")])
def test_explicit_detail_conflict_rejects_without_routes(database,changes):
    create();svc=service();raw=grant(svc,change("replace"));before=row();real=svc.provider.execute
    def execute(name,args):
        r=real(name,args)
        return r.model_copy(update={"data":r.data.model_copy(update=changes)}) if name=="get_poi" else r
    svc.provider.execute=execute;result=svc.replan(PlanChangeRequest(**raw))
    assert (result.decision,result.reason)==("rejected","GROUNDING_CONFLICT")
    assert row()==before and not any(n=="get_driving_route" for n,a in svc.provider.calls)


def test_detail_timeout_still_unverifiable(database):
    create();svc=service();raw=grant(svc,change("replace"));before=row();real=svc.provider.execute
    def execute(name,args):
        return failure_result(name,"timeout","TIMEOUT",query_hash=build_tool_query_key(name,args)) if name=="get_poi" else real(name,args)
    svc.provider.execute=execute;result=svc.replan(PlanChangeRequest(**raw))
    assert result.decision=="unverifiable" and row()==before


def test_remove_unique_must_visit_still_early(database):
    plan=base_plan();plan.constraints.must_visit=["B"];create(attach_facts(plan));svc=service();before=row()
    result=svc.replan(PlanChangeRequest(**change()))
    assert result.reason=="CONSTRAINT_CONFLICT" and not svc.provider.calls and row()==before


@pytest.mark.parametrize("matching",[True,False])
def test_replace_must_visit_waits_for_selected_candidate(database,matching):
    plan=base_plan();plan.constraints.must_visit=["B"];create(attach_facts(plan))
    svc=service(FakeProvider(items=(poi("X").model_copy(update={"name":"B" if matching else "X"}),)))
    raw=grant(svc,change("replace"));before=row();result=svc.replan(PlanChangeRequest(**raw))
    if matching:
        assert result.decision=="accepted" and result.itinerary.days[0].spots[1].poi_id=="X"
    else:
        assert result.reason=="CONSTRAINT_CONFLICT" and row()==before
        assert [n for n,a in svc.provider.calls]==["search_poi"]


def test_other_preserved_activity_satisfies_must_visit(database):
    plan=base_plan();plan.constraints.must_visit=["B"];plan.days[0].spots[2].name="B"
    create(attach_facts(plan));svc=service();raw=grant(svc,change("replace"))
    result=svc.replan(PlanChangeRequest(**raw));assert result.decision=="accepted",result


def test_full_validator_still_checks_must_visit(database,monkeypatch):
    from app.services import replanning_service
    plan=base_plan();plan.constraints.must_visit=["B"];create(attach_facts(plan));svc=service();raw=grant(svc,change("replace"));before=row()
    monkeypatch.setattr(replanning_service,"precheck_constraints",lambda *a:None)
    result=svc.replan(PlanChangeRequest(**raw))
    assert result.decision=="rejected" and row()==before
    assert any(v.type=="must_visit_missing" for v in result.validation_report.violations)


def test_unrelated_legacy_duplicate_does_not_block_time_edit(database):
    plan=base_plan();plan.days[1].spots[1].name=" e ";state=create(attach_facts(plan));svc=service()
    result=svc.replan(PlanChangeRequest(**change("modify_time")))
    assert result.decision=="accepted" and result.itinerary.days[1]==state.itinerary.days[1]


@pytest.mark.parametrize("kind",["add","replace"])
def test_new_normalized_duplicate_is_rejected(database,kind):
    create();svc=service();raw=grant(svc,change(kind));g=svc.store.get(raw["candidate_set_id"])
    from app.models.replanning import GroundedCandidate
    bad=GroundedCandidate(poi=poi("X").model_copy(update={"name":" a "}),estimated_cost=0)
    svc.store.put(g.model_copy(update={"candidates":(bad,)}));before=row()
    result=svc.replan(PlanChangeRequest(**raw))
    assert result.reason=="DUPLICATE_ATTRACTION" and row()==before


def test_remove_can_resolve_existing_same_day_duplicate(database):
    plan=base_plan();plan.days[0].spots[2].name="B";create(attach_facts(plan))
    result=service().replan(PlanChangeRequest(**change()))
    assert result.decision=="accepted",result


@pytest.mark.parametrize("variant,eligible,actual",[("fresh",4,4),("stale",3,3),("unsupported",4,0),("bad_snapshot",0,0)])
def test_actual_reuse_not_theoretical_eligibility(database,variant,eligible,actual):
    plan=base_plan()
    if variant=="unsupported":plan.constraints.transport_preference="walking"
    create(attach_facts(plan,stale=["E>F"] if variant=="stale" else [],bad_fingerprint=variant=="bad_snapshot"))
    result=service().replan(PlanChangeRequest(**change("modify_time")))
    assert len(result.scope.eligible_reuse_queries)==eligible
    assert len(result.scope.reused_queries)==actual
    if variant=="stale":assert len(result.scope.recollected_queries)==1
    if variant=="unsupported":assert len(result.scope.unknown_queries)==4


def test_expiring_eligible_observation_not_counted_as_reused(database):
    create();svc=service();times=[NOW];svc.clock=lambda:times[0]
    svc.provider.hook=lambda *a:times.__setitem__(0,NOW+timedelta(seconds=950))
    result=svc.replan(PlanChangeRequest(**change()))
    assert len(result.scope.eligible_reuse_queries)==2 and result.scope.reused_queries==()
    assert result.decision=="unverifiable"


def test_same_query_multiple_pairs_counts_one_observation(database):
    plan=base_plan();plan.days[1].spots[0].longitude=plan.days[0].spots[2].longitude
    plan.days[1].spots[1].longitude=plan.days[0].spots[3].longitude
    create(attach_facts(plan));result=service().replan(PlanChangeRequest(**change("modify_time")))
    assert result.decision=="accepted"
    assert len(result.scope.reused_queries)==3
    assert len(result.itinerary.planning_facts.route_facts)==4


def test_time_reorder_preserves_existing_transport_identity(database):
    create(attach_facts(transport_plan()));result=service().replan(PlanChangeRequest(**change("modify_time",start_time="17:00",end_time="18:00")))
    assert result.decision=="accepted"
    assert [(t.from_place,t.to_place,t.estimated_cost) for t in result.itinerary.days[0].transport[:2]]==[("A","B",50),("B","C",50)]


def test_route_disabled_does_not_disable_hard_budget_evidence(database):
    plan=base_plan();plan.constraints.require_route_feasibility=False;plan.constraints.transport_preference=None
    create(attach_facts(plan));before=row();svc=service()
    result=svc.replan(PlanChangeRequest(**change()))
    assert result.decision=="unverifiable" and row()==before and svc.provider.calls==[]
    assert any(v.type=="budget_unverifiable" for v in result.validation_report.unknowns)


def test_no_hard_budget_allows_unknown_cost_but_reports_it(database,monkeypatch):
    plan=base_plan();plan.constraints.budget_limit=None;create(attach_facts(plan))
    monkeypatch.setattr(transport_policy,"estimate_transport",lambda *a:None)
    result=service().replan(PlanChangeRequest(**change()))
    assert result.decision=="accepted" and result.change_summary["transport_cost_status"]=="unknown"
    assert result.itinerary.days[0].transport==[]


def test_ambiguous_must_visit_identity_is_not_assumed_satisfied(database):
    plan=base_plan();plan.constraints.must_visit=[PlaceReference(entity_id="unproven-id",name="B")]
    plan.days[0].spots[1].poi_id=None
    create(plan);before=row();svc=service();raw=grant(svc,change("replace"))
    result=svc.replan(PlanChangeRequest(**raw))
    assert result.decision in ("rejected","unverifiable") and row()==before


def test_same_day_existing_duplicate_survives_time_only_delta(database):
    plan=base_plan();plan.days[0].spots[2].name="B";create(attach_facts(plan))
    result=service().replan(PlanChangeRequest(**change("modify_time")))
    assert result.decision=="accepted"


def test_independent_policy_rejects_unrelated_cost_mutation(database):
    state=create();request=PlanChangeRequest(**change());p=patches.build_patch(state,request)
    candidate=patches.merge_patch(state,authorize_scope(state,request),p)
    candidate.budget_breakdown.transport+=1
    with pytest.raises(StateError,match="UNAUTHORIZED_PLAN_MUTATION"):
        patches.assert_preserved_projection(state.itinerary,candidate,p)
