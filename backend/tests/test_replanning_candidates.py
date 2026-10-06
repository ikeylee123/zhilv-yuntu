import pytest
from datetime import timedelta
from test_authoritative_state import database
from eval.replanning_support import *
from app.models.replanning import PlanChangeRequest, StateError
from app.services.replanning_candidates import authorized_candidate, CandidateStore

@pytest.mark.parametrize("field,value",[("trip_id","other"),("base_plan_version",2),("target_day",2),("operation","add"),("target_activity_id","activity-C"),("created_at",NOW-timedelta(seconds=301)),("fetched_at",NOW+timedelta(seconds=1))])
def test_binding_and_expiry(database,field,value):
    state=create();svc=service();raw=grant(svc,change("replace"));g=svc.store.get(raw["candidate_set_id"])
    svc.store.put(g.model_copy(update={field:value}))
    with pytest.raises(StateError): authorized_candidate(state,PlanChangeRequest(**raw),svc.store,NOW)

@pytest.mark.parametrize("candidate",["arbitrary","A","not-in-list"])
def test_arbitrary_provider_id_not_accepted(database,candidate):
    create();svc=service();raw=grant(svc,change("replace"));svc.provider.calls.clear();raw["candidate_id"]=candidate
    result=svc.replan(PlanChangeRequest(**raw))
    assert result.reason=="CANDIDATE_NOT_AUTHORIZED" and svc.provider.calls==[]


def test_filters_conflicts_region_selected_avoid(database):
    plan=base_plan();plan.constraints.avoid=["Y"];create(attach_facts(plan))
    items=[poi("X"),poi("X").model_copy(update={"name":"conflict"}),poi("A"),poi("Y"),poi("Z").model_copy(update={"adcode":"110101"}),poi("W")]
    svc=service(FakeProvider(items=items));raw=grant(svc,change("replace"))
    assert [c.poi.poi_id for c in svc.store.get(raw["candidate_set_id"]).candidates]==["W"]


def test_grounding_drift(database):
    create();svc=service();raw=grant(svc,change("replace"));svc.provider.items=(poi("X").model_copy(update={"name":"drift"}),)
    result=svc.replan(PlanChangeRequest(**raw));assert result.reason=="GROUNDING_CONFLICT"


def test_bounded_store(database):
    create();svc=service();raw=grant(svc,change("replace"));g=svc.store.get(raw["candidate_set_id"])
    store=CandidateStore(capacity=2)
    for i in range(3):store.put(g.model_copy(update={"candidate_set_id":str(i)}))
    with pytest.raises(StateError):store.get("0")


def test_curated_city_resolves_missing_adcode(monkeypatch):
    from types import SimpleNamespace
    from app.services.replanning_candidates import default_region
    from app.services import city_resolver_service, map_service
    monkeypatch.setattr(city_resolver_service,"resolve_city",lambda _:SimpleNamespace(adcode=None,city="Curated"))
    calls=[]
    monkeypatch.setattr(map_service,"resolve_administrative_area",lambda city:calls.append(city) or {"adcode":"330100"})
    assert default_region("input").adcode=="330100" and calls==["Curated"]


def test_candidate_api_two_step(database):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes.trip import router,get_replanning_service
    from app.models.replanning import CandidatePreparationRequest
    create();svc=service();app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_replanning_service]=lambda:svc
    client=TestClient(app);raw=change("replace")
    prep={k:v for k,v in raw.items() if k in CandidatePreparationRequest.model_fields}
    response=client.post("/trip/fixture/replan/candidates",json=prep)
    assert response.status_code==200,response.text
    raw["candidate_set_id"]=response.json()["candidate_set_id"]
    committed=client.post("/trip/fixture/replan",json=raw)
    assert committed.json()["decision"]=="accepted"
