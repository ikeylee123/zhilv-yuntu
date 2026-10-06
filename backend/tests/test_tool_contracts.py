from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import pytest
from pydantic import ValidationError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.models.tool_contracts import *
ARGS=dict(origin=dict(latitude=30,longitude=120),destination=dict(latitude=30.1,longitude=120.1))
NOW=datetime(2026,10,6,tzinfo=timezone.utc)
def success():
    return ToolResult[DrivingRouteData](status='success',data=DrivingRouteData(**ARGS,distance_meters=1200,duration_seconds=600),source=source_for('get_driving_route'),fetched_at=NOW,metadata=ToolMetadata(request_id='test',query_hash=build_tool_query_key('get_driving_route',ARGS)))
@pytest.mark.parametrize('value',[True,False,-1,'NaN','Infinity','-Infinity','','bad'])
@pytest.mark.parametrize('field',['distance_meters','duration_seconds'])
def test_bad_number(field,value):
    raw=success().data.model_dump();raw[field]=value
    with pytest.raises(ValidationError):DrivingRouteData.model_validate(raw)
@pytest.mark.parametrize('value',[True,False,'NaN','Infinity',181])
def test_bad_coordinates(value):
    with pytest.raises(ValidationError):Coordinates(latitude=0,longitude=value)
@pytest.mark.parametrize('value',['1200','1200.5',0,0.0,1200,1200.5])
def test_good_number(value):
    assert DrivingRouteData(**ARGS,distance_meters=value,duration_seconds=value).duration_seconds==float(value)
@pytest.mark.parametrize('change',[dict(data=None),dict(fetched_at=None),dict(fetched_at='2026-10-06T00:00:00'),dict(reason='bad'),dict(status='timeout'),dict(source={}),dict(schema_version='old')])
def test_success_invariants(change):
    raw=success().model_dump();raw.update(change)
    with pytest.raises(ValidationError):ToolResult[DrivingRouteData].model_validate(raw)
@pytest.mark.parametrize('status',[s for s in ToolStatus if s!='success'])
def test_failure_invariants(status):
    r=failure_result('get_driving_route',status,status.value.upper());assert r.data is None
    raw=r.model_dump();raw['data']=success().data.model_dump()
    with pytest.raises(ValidationError):ToolResult[DrivingRouteData].model_validate(raw)
def test_roundtrip_and_frozen():
    r=success();assert ToolResult[DrivingRouteData].model_validate_json(r.model_dump_json())==r
    with pytest.raises(ValidationError):r.data.origin.latitude=10
@pytest.mark.parametrize('name,change',[('get_driving_route',dict(mode='walking')),('get_driving_route',dict(strategy=1)),('search_poi',dict(page=2)),('search_poi',dict(category='hotel')),('search_poi',dict(page_size=2)),('search_poi',dict(city_limit=False)),('get_poi',dict(poi_id='b'))])
def test_query_sensitive(name,change):
    args=ARGS if name=='get_driving_route' else dict(poi_id='a') if name=='get_poi' else dict(query='Museum',city_or_adcode='330100',category='spot')
    assert build_tool_query_key(name,args)!=build_tool_query_key(name,dict(args,**change))
def test_direction_versions_normalization():
    key=build_tool_query_key('get_driving_route',ARGS)
    assert key==build_tool_query_key('get_driving_route',dict(reversed(list(ARGS.items()))))
    assert key!=build_tool_query_key('get_driving_route',dict(origin=ARGS['destination'],destination=ARGS['origin']))
    for field in ['provider','api_version','policy_version','schema_version']:
        assert key!=build_tool_query_key('get_driving_route',ARGS,**{field:'different'})
    changed=deepcopy(ARGS);changed['origin']['longitude']+=1e-10
    assert key!=build_tool_query_key('get_driving_route',changed)
    a=success();b=a.model_copy(update={'metadata':a.metadata.model_copy(update={'request_id':'other'})})
    assert a.metadata.query_hash==b.metadata.query_hash
    with pytest.raises(ValidationError):build_tool_query_key('get_poi',{'poi_id':'a','request_id':'extra'})
