from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
import json
import sys
import pytest
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.tool_adapter import DirectToolProvider,ReplayToolProvider,MapBackend
from app.services.tool_replay import ReplayBackend
from app.models.tool_contracts import *
from test_tool_contracts import ARGS,NOW,success
from route_fixtures import state,trusted
from app.services.planning_fact_service import collect_route_facts
from app.services.plan_validator import validate_plan
CASES=json.loads((Path(__file__).resolve().parents[1]/'eval/mcp_contract_cases.json').read_text())['cases']
def make(case,**kwargs):
    defaults=dict(backend=ReplayBackend(case),cache_get=lambda k:None,cache_set=lambda *a,**k:None,clock=lambda:NOW)
    defaults.update(kwargs);return DirectToolProvider(**defaults)
@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_raw_replay(case):
    p=make(case);r=p.execute(case['tool'],case['arguments'])
    assert {'status':r.status.value,'reason':r.reason}==case['expected']
    assert r.metadata.provider_attempts==(0 if r.status=='unsupported' else 1)
@pytest.mark.parametrize('outcome',['success','timeout','no_data','provider_error','invalid_response'])
def test_inflight_dedup(outcome):
    started,release=Event(),Event()
    class Backend:
        calls=0
        def route(self,request):
            self.calls+=1;started.set();assert release.wait(5)
            if outcome=='timeout':raise httpx.ReadTimeout('secret')
            if outcome=='provider_error':raise RuntimeError('secret')
            if outcome=='no_data':return None
            return dict(distance_meters=True if outcome=='invalid_response' else 1200,duration_seconds=600)
    b=Backend();p=make(CASES[0],backend=b)
    with ThreadPoolExecutor(max_workers=8) as pool:
        first=pool.submit(p.execute,'get_driving_route',ARGS);assert started.wait(5)
        pending=[pool.submit(p.execute,'get_driving_route',ARGS) for _ in range(7)]
        release.set();results=[first.result()]+[f.result() for f in pending]
    assert b.calls==1 and all(r.status==outcome for r in results)
    assert sum(r.metadata.provider_attempts for r in results)==1
    assert sum(r.metadata.deduplicated for r in results)==7
    assert p.execute('get_driving_route',ARGS).metadata.deduplicated
    assert all('secret' not in r.model_dump_json() for r in results)
def test_separate_keys_and_runs():
    class Backend:
        calls=0
        def route(self,request):self.calls+=1;return dict(distance_meters=1,duration_seconds=1)
    b=Backend();p=make(CASES[0],backend=b);p.execute('get_driving_route',ARGS)
    p.execute('get_driving_route',dict(origin=ARGS['destination'],destination=ARGS['origin']))
    make(CASES[0],backend=b).execute('get_driving_route',ARGS)
    assert b.calls==3
@pytest.mark.parametrize('variant',['fresh','stale','future','missing_time','missing_schema','missing_policy','missing_source','bool','wrong_key','legacy'])
def test_cache_provenance(variant):
    raw=success().model_dump(mode='json')
    if variant=='stale':raw['fetched_at']=(NOW-timedelta(seconds=901)).isoformat()
    if variant=='future':raw['fetched_at']=(NOW+timedelta(seconds=1)).isoformat()
    if variant=='missing_time':raw.pop('fetched_at')
    if variant=='missing_schema':raw.pop('schema_version')
    if variant=='missing_policy':raw['metadata'].pop('policy_version')
    if variant=='missing_source':raw.pop('source')
    if variant=='bool':raw['data']['duration_seconds']=False
    if variant=='wrong_key':raw['metadata']['query_hash']='0'*64
    if variant=='legacy':raw={'distance_meters':1,'duration_seconds':0}
    p=make(CASES[0],cache_get=lambda k:raw);r=p.execute('get_driving_route',ARGS)
    assert r.status=='success' and r.metadata.cache_hit==(variant=='fresh')
    assert p.backend.calls==(0 if variant=='fresh' else 1)
    if variant=='fresh':assert r.fetched_at==NOW
@pytest.mark.parametrize('args',[dict(poi_id=''),dict(poi_id='a',url='http://evil'),dict(poi_id='a'*257)])
def test_invalid_argument(args):
    p=make(CASES[0]);r=p.execute('get_poi',args)
    assert r.status=='invalid_argument' and p.backend.calls==0
def test_replay_missing_no_network(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',lambda *a:pytest.fail('network'))
    key=build_tool_query_key('get_driving_route',ARGS);p=ReplayToolProvider({key:success().model_dump(mode='json')})
    assert p.execute('get_driving_route',ARGS).status=='success'
    assert p.execute('get_poi',{'poi_id':'missing'}).reason=='REPLAY_MISS'
    assert ReplayToolProvider({key:{}}).execute('get_driving_route',ARGS).reason=='INVALID_REPLAY'
@pytest.mark.parametrize('duration,status',[(600,'valid'),(2700,'invalid')])
def test_collector_replay(duration,status):
    r=success().model_dump(mode='json');r['data']['duration_seconds']=duration
    p=ReplayToolProvider({build_tool_query_key('get_driving_route',ARGS):r})
    s=state();s.planning_facts=collect_route_facts(s.current_itinerary,s.constraints,trusted_pois=trusted(s),now=NOW,tool_provider=p)
    assert validate_plan(s).status==status
@pytest.mark.parametrize('age,reason',[(901,'stale_fact'),(-1,'invalid_response')])
def test_collector_freshness(age,reason):
    r=success().model_dump(mode='json');r['fetched_at']=(NOW-timedelta(seconds=age)).isoformat()
    p=ReplayToolProvider({build_tool_query_key('get_driving_route',ARGS):r})
    s=state();s.planning_facts=collect_route_facts(s.current_itinerary,s.constraints,trusted_pois=trusted(s),now=NOW,tool_provider=p)
    assert validate_plan(s).status=='unverifiable' and s.planning_facts.route_facts[0].reason==reason
def test_logs(caplog):
    import logging
    caplog.set_level(logging.INFO,logger='app.services.tool_adapter')
    make(CASES[0]).execute('get_driving_route',ARGS)
    assert 'query_hash' in caplog.text and 'provider_attempts' in caplog.text
    assert '120.1' not in caplog.text
@pytest.mark.parametrize('category,expected',[('spot','\u98ce\u666f\u540d\u80dc'),('meal','\u9910\u996e\u670d\u52a1'),('hotel','\u4f4f\u5bbf\u670d\u52a1')])
def test_search_uses_provider_and_bypasses_old_cache(monkeypatch,category,expected):
    from app.services import map_service
    observed=[]
    monkeypatch.setattr(map_service,'search_places',lambda **kw:observed.append(kw) or [])
    r=make(CASES[0],backend=MapBackend()).search_poi(query='Museum',city_or_adcode='330100',category=category)
    assert r.status=='no_data' and observed[0]['types']==expected and observed[0]['use_cache'] is False
