import pytest
from pydantic import ValidationError
from test_authoritative_state import database
from eval.replanning_support import *
from app.models.replanning import PlanChangeRequest, StateError
from app.services.replanning_scope import authorize_scope

@pytest.mark.parametrize("extra",[dict(coordinates={}),dict(constraints={}),dict(validation_report={}),dict(planning_facts={}),dict(destination="x"),dict(activity_id="new"),dict(budget=0),dict(state_schema_version=1),dict(targets=[]),dict(base_plan_version=True),dict(base_plan_version="1"),dict(target_day_index=True),dict(change_type="move"),dict(start_time="09:00"),dict(candidate_id="X"),dict(append=True)])
def test_strict_request(extra):
    with pytest.raises(ValidationError): PlanChangeRequest(**dict(change(),**extra))

@pytest.mark.parametrize("extra,code",[(dict(trip_id="other"),"INVALID_CHANGE_REQUEST"),(dict(base_plan_version=2),"VERSION_CONFLICT"),(dict(target_activity_id="fake"),"ACTIVITY_NOT_FOUND"),(dict(target_activity_id="activity-E"),"ACTIVITY_DAY_MISMATCH"),(dict(target_day_index=5),"ACTIVITY_DAY_MISMATCH")])
def test_scope_reject(database,extra,code):
    state=create()
    with pytest.raises(StateError,match=code): authorize_scope(state,PlanChangeRequest(**dict(change(),**extra)))

@pytest.mark.parametrize("anchor",["activity-A","activity-D"])
def test_add_anchor(database,anchor):
    state=create();scope=authorize_scope(state,PlanChangeRequest(**change("add",before_activity_id=anchor)))
    assert scope.affected_activity_ids==()
    assert len(scope.preserved_activity_ids)==6

@pytest.mark.parametrize("extra",[dict(before_activity_id=None,append=False),dict(append=True),dict(start_time="25:00"),dict(end_time="09:00"),dict(target_activity_id="activity-A")])
def test_malformed_add(extra):
    with pytest.raises(ValidationError): PlanChangeRequest(**dict(change("add"),**extra))
