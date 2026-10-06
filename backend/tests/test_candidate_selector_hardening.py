"""Independent selector boundary regressions; frozen evaluator labels are not used."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from pydantic import ValidationError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_authoritative_state import database
from test_candidate_selector import ReplayLLM, selection_data
from test_replanning_commit import row
from test_replanning_llm_selection import request, wrapper
from eval.replanning_support import create, service
from app.agents import candidate_selector
from app.agents.candidate_selector import CandidateSelector
from app.models.candidate_selection import CandidateSelectionInput, TargetContext
from app.services import replanning_selection


@pytest.mark.parametrize("content", [
    '['*3000+'0'+']'*3000,
    '{"x":'*1200+'0'+'}'*1200,
    '{',
    '{"candidate_id":"X","candidate_id":"evil"}',
    '{"candidate_id":"X"}{"candidate_id":"evil"}',
    '```json\n{"candidate_id":"X"}\n```',
    '{"candidate_id":"X"} trailing',
    '"X"', '123', 'true', 'null', '[{"candidate_id":"X"}]',
    '{"candidate_id":"X","coordinates":[]}',
    '{"candidate_id":"X","rationale":"\\ud800"}',
])
def test_unsafe_output_is_invalid(content):
    client=ReplayLLM(content)
    result=CandidateSelector(client).select(selection_data())
    assert result.error=="LLM_SELECTION_INVALID" and result.output is None
    assert len(client.calls)==1


@pytest.mark.parametrize("content",['{"candidate_id":"X"}',
    '{"candidate_id":"X","rationale":null}',
    '{"candidate_id":"X","rationale":"history preference"}'])
def test_valid_output_unchanged(content):
    client=ReplayLLM(content);result=CandidateSelector(client).select(selection_data())
    assert result.error is None and result.output.candidate_id=="X" and len(client.calls)==1


@pytest.mark.parametrize("error",[RecursionError(),UnicodeError(),TypeError(),ValueError()])
def test_parse_boundary_exception_mapping(monkeypatch,error):
    def fail(*args,**kwargs): raise error
    monkeypatch.setattr(candidate_selector.json,"loads",fail)
    result=CandidateSelector(ReplayLLM()).select(selection_data())
    assert result.error=="LLM_SELECTION_INVALID"


def test_parser_programming_error_not_swallowed(monkeypatch):
    def fail(*args,**kwargs): raise RuntimeError("parser programming error")
    monkeypatch.setattr(candidate_selector.json,"loads",fail)
    with pytest.raises(RuntimeError,match="parser programming error"):
        CandidateSelector(ReplayLLM()).select(selection_data())


@pytest.mark.parametrize("error,reason",[(TimeoutError(),"LLM_SELECTION_TIMEOUT"),
    (RuntimeError(),"LLM_SELECTION_PROVIDER_ERROR")])
def test_provider_errors_stay_distinct(error,reason):
    assert CandidateSelector(ReplayLLM(error=error)).select(selection_data()).error==reason


def test_membership_failure_stays_distinct():
    assert CandidateSelector(ReplayLLM('{"candidate_id":"evil"}')).select(selection_data()).error=="LLM_SELECTION_NOT_AUTHORIZED"


INVALID_SLOTS=[(None,None),(None,"10:00"),("09:00",None),("10:00","10:00"),
    ("11:00","10:00"),("23:00","01:00"),("25:90","10:00"),("09:00","yesterday"),
    ("9am","10:00"),("09:60","10:00"),("","10:00"),(" ","10:00"),
    ("9:00","10:00"),("09:00","24:00"),(True,"10:00"),(900,"10:00")]


@pytest.mark.parametrize("start,end",INVALID_SLOTS)
def test_bad_add_context_zero_llm(start,end):
    raw=selection_data().model_dump(mode="json")
    raw.update(operation="add",target_context={"authorized_start_time":start,"authorized_end_time":end})
    client=ReplayLLM();selector=CandidateSelector(client)
    with pytest.raises(ValidationError):
        selector.select(CandidateSelectionInput.model_validate_json(json.dumps(raw)))
    assert client.calls==[]


@pytest.mark.parametrize("operation,start,end",[
    ("replace",None,None),("replace","09:00","09:30"),
    ("add","09:00","09:30"),("add","00:00","23:59"),
])
def test_valid_context(operation,start,end):
    data=selection_data(operation=operation,target_context=TargetContext(
        authorized_start_time=start,authorized_end_time=end))
    client=ReplayLLM()
    assert CandidateSelector(client).select(data).output.candidate_id=="X"
    assert len(client.calls)==1


@pytest.mark.parametrize("start,end",INVALID_SLOTS[1:])
def test_replace_optional_context_must_be_valid_pair(start,end):
    with pytest.raises(ValidationError):
        selection_data(target_context=TargetContext(authorized_start_time=start,authorized_end_time=end))


def test_selector_revalidates_constructed_input_before_call():
    data=selection_data().model_copy(update={"operation":"add"})
    client=ReplayLLM()
    with pytest.raises(ValidationError): CandidateSelector(client).select(data)
    assert client.calls==[]


def http_setup(monkeypatch,content):
    from app.api.routes.trip import router,get_replanning_service
    core=service();client=ReplayLLM(content);selector=CandidateSelector(client)
    selector.select=Mock(wraps=selector.select)
    core.committer=Mock(wraps=core.committer)
    monkeypatch.setattr(replanning_selection,"CandidateSelector",lambda:selector)
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_replanning_service]=lambda:core
    return TestClient(app,raise_server_exceptions=False),core,client,selector


@pytest.mark.parametrize("content",['['*3000+'0'+']'*3000,'{"x":'*1200+'0'+'}'*1200])
def test_deep_json_http_preserves_full_row(database,monkeypatch,content):
    create();before=row();http,core,client,selector=http_setup(monkeypatch,content)
    response=http.post('/trip/fixture/replan',json=request().model_dump(mode='json'))
    assert response.status_code==200,response.text
    result=response.json()
    assert (result['decision'],result['reason'])==('rejected','LLM_SELECTION_INVALID')
    assert len(client.calls)==selector.select.call_count==1
    assert [n for n,a in core.provider.calls]==['search_poi']
    core.committer.assert_not_called()
    assert row()==before and row()['planning_version']==1
    assert all(word not in response.text for word in ('RecursionError','Traceback','JSONDecodeError'))
    assert content not in response.text


@pytest.mark.parametrize("update",[
    {'start_time':None,'end_time':None},{'start_time':'25:90'},
    {'end_time':'yesterday'},{'start_time':None},
    {'start_time':'23:00','end_time':'01:00'},
])
def test_invalid_add_http_zero_calls(database,monkeypatch,update):
    create();before=row();http,core,client,selector=http_setup(monkeypatch,'{"candidate_id":"X"}')
    raw=request('add').model_dump(mode='json');raw.update(update)
    if update=={'start_time':None,'end_time':None}:
        raw.pop('start_time');raw.pop('end_time')
    response=http.post('/trip/fixture/replan',json=raw)
    assert response.status_code==422
    assert client.calls==[] and selector.select.call_count==0 and core.provider.calls==[]
    core.committer.assert_not_called()
    assert row()==before


def test_invalid_projected_context_is_input_failure(database,monkeypatch):
    create();before=row();core=service();client=ReplayLLM()
    original=replanning_selection.selection_input
    def invalid(*args):
        data=original(*args)
        raw=data.model_dump();raw['target_context']['authorized_end_time']=None
        return CandidateSelectionInput.model_validate(raw)
    monkeypatch.setattr(replanning_selection,'selection_input',invalid)
    result=wrapper(core,client).replan(request('add'))
    assert result.reason=='INVALID_CHANGE_REQUEST' and client.calls==[] and row()==before
    assert [n for n,a in core.provider.calls]==['search_poi']


@pytest.mark.parametrize("field",['start_time','end_time','authorized_start_time','authorized_end_time'])
def test_time_never_becomes_model_output(field):
    content=json.dumps({'candidate_id':'X',field:'09:00'})
    assert CandidateSelector(ReplayLLM(content)).select(selection_data()).error=='LLM_SELECTION_INVALID'
