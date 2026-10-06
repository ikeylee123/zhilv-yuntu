import pytest
from test_authoritative_state import database
from eval.replanning_support import *
from app.models.replanning import PlanChangeRequest

@pytest.mark.parametrize("kind,search,detail,routes",[("remove",0,0,1),("modify_time",0,0,0),("replace",1,1,2),("add",1,1,2)])
def test_only_required_tools(database,kind,search,detail,routes):
    create();provider=FakeProvider();svc=service(provider);raw=change(kind)
    if kind in ("replace","add"):raw=grant(svc,raw)
    result=svc.replan(PlanChangeRequest(**raw));assert result.decision=="accepted"
    names=[n for n,a in provider.calls]
    assert (names.count("search_poi"),names.count("get_poi"),names.count("get_driving_route"))==(search,detail,routes)
    assert [a["poi_id"] for n,a in provider.calls if n=="get_poi"]==(["X"] if detail else [])
