import pytest
from test_authoritative_state import database
from eval.replanning_support import *
from app.models.replanning import PlanChangeRequest, StateError
from app.services.replanning_scope import authorize_scope
from app.services.plan_patch_service import build_patch, merge_patch, assert_preserved_projection

@pytest.mark.parametrize("field",["day","activity","destination","travelers","constraints","request","time","poi","budget","summary","version"])
def test_mutation_detected(database,field):
    state=create();request=PlanChangeRequest(**change());patch=build_patch(state,request)
    merged=merge_patch(state,authorize_scope(state,request),patch)
    if field=="day": merged.days[1].theme="tamper"
    if field=="activity": merged.days[0].spots[0].description="tamper"
    if field=="destination": merged.destination="tamper"
    if field=="travelers": merged.original_request.travelers=9
    if field=="constraints": merged.constraints.budget_limit=9999
    if field=="request": merged.original_request.special_notes="tamper"
    if field=="time": merged.days[0].spots[0].end_time="10:30"
    if field=="poi": merged.days[0].spots[0].poi_id="tamper"
    if field=="budget": merged.budget_breakdown.meals=99
    if field=="summary": merged.summary="tamper"
    if field=="version": merged.planning_version=9
    with pytest.raises(StateError,match="UNAUTHORIZED_PLAN_MUTATION"):
        assert_preserved_projection(state.itinerary,merged,patch)
