import pytest
from test_authoritative_state import database
from eval.replanning_support import *
from app.models.replanning import PlanChangeRequest, GroundedCandidate
from app.services.replanning_scope import authorize_scope
from app.services.plan_patch_service import build_patch, merge_patch

@pytest.mark.parametrize("kind",["replace","remove","modify_time"])
@pytest.mark.parametrize("target",list("ABCD"))
def test_positions(database,kind,target):
    state=create();request=PlanChangeRequest(**change(kind,target));before=state.itinerary.model_dump_json()
    patch=build_patch(state,request,GroundedCandidate(poi=poi("X"),estimated_cost=30),id_factory=lambda:"new")
    merged=merge_patch(state,authorize_scope(state,request),patch)
    old=next(a for a in state.itinerary.days[0].spots if a.poi_id==target)
    for d in state.itinerary.days:
        for a in d.spots:
            if a.poi_id!=target: assert a in next(x for x in merged.days if x.day_index==d.day_index).spots
    if kind=="replace":
        new=next(a for a in merged.days[0].spots if a.poi_id=="X")
        assert new.activity_id=="new" and (new.start_time,new.end_time)==(old.start_time,old.end_time)
        assert merged.estimated_budget==80
    elif kind=="remove": assert merged.estimated_budget==50
    else:
        new=next(a for a in merged.days[0].spots if a.poi_id==target)
        assert new.model_dump(exclude={"start_time","end_time"})==old.model_dump(exclude={"start_time","end_time"})
        assert merged.estimated_budget==60
    assert state.itinerary.model_dump_json()==before

@pytest.mark.parametrize("anchor,append,index",[("activity-A",False,0),("activity-B",False,1),(None,True,4)])
def test_insert_positions(database,anchor,append,index):
    state=create();request=PlanChangeRequest(**change("add",before_activity_id=anchor,append=append))
    patch=build_patch(state,request,GroundedCandidate(poi=poi("X"),estimated_cost=30),id_factory=lambda:"new")
    merged=merge_patch(state,authorize_scope(state,request),patch)
    assert merged.days[0].spots[index].activity_id=="new"
    assert merged.estimated_budget==90
