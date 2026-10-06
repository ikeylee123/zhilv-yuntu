from test_authoritative_state import database
from eval.replanning_support import *
from app.models.replanning import PlanChangeRequest
from app.services import storage_service as store
from app.models.db_models import TripRecord
from app.services.authoritative_state import load_authoritative_plan, compare_and_swap_plan
import pytest


def row():
    with store.SessionLocal() as session:
        r=session.query(TripRecord).filter_by(trip_id="fixture").one()
        return {c.name:getattr(r,c.name) for c in TripRecord.__table__.columns}


@pytest.mark.parametrize("kind",["replace","remove","add","modify_time"])
def test_accept_real_cas(database,kind):
    state=create();svc=service();raw=change(kind)
    if kind in ("replace","add"): raw=grant(svc,raw)
    result=svc.replan(PlanChangeRequest(**raw))
    assert result.decision=="accepted",result.model_dump_json()
    assert result.candidate_plan_version==2
    assert result.validation_report.status=="valid"
    assert result.itinerary.days[1]==state.itinerary.days[1]
    assert result.itinerary.days[0].spots[0]==state.itinerary.days[0].spots[0]
    assert row()["planning_version"]==2
    if kind=="modify_time": assert result.itinerary.days[0].spots[1].activity_id=="activity-B"


@pytest.mark.parametrize("mode,decision",[("route_gap","rejected"),("must_visit","rejected"),("timeout","unverifiable"),("overlap","rejected")])
def test_failed_candidate_preserves_entire_row(database,mode,decision):
    plan=base_plan()
    if mode=="must_visit": plan.constraints.must_visit=["B"]
    create(attach_facts(plan));before=row();provider=FakeProvider(timeout=mode=="timeout");svc=service(provider)
    raw=change("modify_time",start_time="10:05",end_time="10:50") if mode=="route_gap" else change()
    if mode=="overlap": raw=grant(svc,change("add",start_time="09:30",end_time="10:30"))
    result=svc.replan(PlanChangeRequest(**raw))
    assert result.decision==decision,result
    assert row()==before
    if mode=="must_visit": assert provider.calls==[]


def test_competing_writer_during_tools(database):
    state=create();provider=FakeProvider();svc=service(provider);winner=[]
    def hook(name,args):
        if name=="get_driving_route" and not winner:
            candidate=state.itinerary.model_copy(deep=True)
            candidate.summary="winning writer"
            compare_and_swap_plan("fixture",1,candidate,evaluation_time=NOW)
            winner.append(row())
    provider.hook=hook
    result=svc.replan(PlanChangeRequest(**change()))
    assert result.decision=="conflict"
    assert row()==winner[0]


def test_api_strict_body_and_path(database):
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.api.routes.trip import router
    app=FastAPI();app.include_router(router)
    from app.api.routes.trip import get_replanning_service
    create();svc=service();app.dependency_overrides[get_replanning_service]=lambda:svc
    try:
        client=TestClient(app)
        assert client.post("/trip/fixture/replan",json=dict(change(),coordinates={})).status_code==422
        assert client.post("/trip/other/replan",json=change()).status_code==422
        response=client.post("/trip/fixture/replan",json=change())
        assert response.status_code==200,response.text
        assert response.json()["decision"]=="accepted"
    finally: app.dependency_overrides.pop(get_replanning_service,None)


def test_legacy_rejection_does_not_lazy_migrate(database):
    import json
    with store.SessionLocal() as session:
        session.add(TripRecord(trip_id="fixture",destination="Fixture City",summary="legacy",
            itinerary_json=json.dumps(base_plan().model_dump(mode="json"))))
        session.commit()
    before=row();svc=service()
    result=svc.replan(PlanChangeRequest(**change()))
    assert result.reason=="REPLAN_NOT_ELIGIBLE"
    assert row()==before and svc.provider.calls==[]
