"""Offline fixtures only: typed fake provider and explicit server evidence."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from app.models.schemas import Itinerary, TripRequest, TripConstraints
from app.models.planning_facts import PlanningFacts, FactMetadata, FactSource, RouteFact
from app.models.tool_contracts import (POIData, POISearchResult, DrivingRouteData, RESULTS,
    ToolMetadata, source_for, build_tool_query_key, failure_result)
from app.services.route_validation import discover_pairs, plan_fingerprint
from app.services.authoritative_state import create_authoritative_draft
from app.services.replanning_candidates import CandidateStore
from app.services.replanning_service import ReplanningService

NOW = datetime(2026,10,6,0,0,tzinfo=timezone.utc)


def poi(name):
    return POIData(poi_id=name, name=name, coordinates=dict(longitude=120+ord(name)/10000,latitude=30),
        category="spot", adcode="330102", address="fixture address")


def base_plan():
    request = TripRequest(destination="Fixture City", start_date="2026-10-06", end_date="2026-10-07", travelers=1,budget=1000)
    days = []
    for n, letters in enumerate(("ABCD","EF"),1):
        spots = []
        for i, name in enumerate(letters):
            p = poi(name)
            spots.append(dict(activity_id="activity-"+name,poi_id=name,name=name,
                longitude=p.coordinates.longitude,latitude=30,start_time=f"{9+i*2:02}:00",
                end_time=f"{10+i*2:02}:00",estimated_cost=10,description="preserve "+name))
        days.append(dict(day_index=n,date=f"2026-10-0{5+n}",spots=spots,theme="preserve day"))
    return Itinerary(trip_id="fixture",destination=request.destination,summary="preserve summary",
        days=days,original_request=request,constraints=TripConstraints(budget_limit=1000,
        require_route_feasibility=True,transport_preference="driving"),estimated_budget=60,
        budget_breakdown=dict(tickets=60,total=60))


def attach_facts(plan, *, stale=(), missing=(), bad_fingerprint=False, duration=600):
    facts=[]
    for pair in discover_pairs(plan)[0]:
        label=pair.origin.entity_id+">"+pair.destination.entity_id
        if label in missing: continue
        facts.append(RouteFact(fact_id=label,status="known",origin=pair.origin,destination=pair.destination,
            distance_meters=1000,duration_seconds=duration,
            source=FactSource(fetched_at=NOW-timedelta(seconds=1000 if label in stale else 60))))
    plan.planning_facts=PlanningFacts(metadata=FactMetadata(run_id="old",collected_at=NOW,
        plan_fingerprint="wrong" if bad_fingerprint else plan_fingerprint(plan,plan.constraints)),route_facts=tuple(facts))
    return plan


def create(plan=None):
    plan=plan or attach_facts(base_plan())
    ids=iter(s.activity_id for d in plan.days for s in d.spots)
    return create_authoritative_draft(plan,id_factory=lambda:next(ids),trip_id_factory=lambda:plan.trip_id)


class FakeProvider:
    def __init__(self, *, items=None, timeout=False, duration=600):
        self.items=tuple(items) if items is not None else (poi("X"),poi("Y"))
        self.calls=[]
        self.timeout,self.duration=timeout,duration
        self.hook=None

    def execute(self,name,args):
        self.calls.append((name,args))
        if self.hook: self.hook(name,args)
        key=build_tool_query_key(name,args)
        if self.timeout and name=="get_driving_route":
            return failure_result(name,"timeout","TIMEOUT",query_hash=key,attempts=1)
        if name=="search_poi": data=POISearchResult(items=self.items,page=args["page"],page_size=args["page_size"])
        elif name=="get_poi": data=next((p for p in self.items if p.poi_id==args["poi_id"]),poi("X"))
        else: data=DrivingRouteData(**args,distance_meters=1000,duration_seconds=self.duration)
        return RESULTS[name](status="success",data=data,source=source_for(name),fetched_at=NOW,
            metadata=ToolMetadata(request_id="fake",query_hash=key,provider_attempts=1))


def service(provider=None, **kwargs):
    return ReplanningService(provider=provider or FakeProvider(), store=CandidateStore(),clock=lambda:NOW,
        id_factory=lambda:"activity-X-new",region_resolver=lambda _:SimpleNamespace(adcode="330100"),**kwargs)


def change(kind="remove",target="B",**kwargs):
    result=dict(trip_id="fixture",base_plan_version=1,change_type=kind,target_day_index=1)
    if kind != "add": result["target_activity_id"]="activity-"+target
    if kind in ("replace","add"): result.update(candidate_set_id="grant",candidate_id="X")
    if kind=="modify_time": result.update(start_time="11:10",end_time="12:00")
    if kind=="add": result.update(before_activity_id="activity-B",start_time="10:15",end_time="10:45")
    result.update(kwargs)
    return result


def grant(svc, raw):
    from app.models.replanning import CandidatePreparationRequest
    request=CandidatePreparationRequest(**{k:v for k,v in raw.items() if k in CandidatePreparationRequest.model_fields})
    allowed=svc.prepare(request)
    return dict(raw,candidate_set_id=allowed.candidate_set_id)
