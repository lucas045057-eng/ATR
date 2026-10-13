import hashlib
from pathlib import Path

import pytest
from app.business.flows import build_flows
from app.business.languages import describe_language
from app.business.profile import build_static


def source_files(sources):
    return [{'id': str(index), 'artifact_id': 'original', 'path': name, 'media_type': 'code',
             'content_digest': hashlib.sha256(code.encode()).hexdigest(),
             'analysis': describe_language(name, code)} for index, (name, code) in enumerate(sources.items())]


@pytest.mark.parametrize('name,code,symbol', [
    ('api.py', 'from fastapi import FastAPI\napp=FastAPI()\n@app.get("/record")\ndef record():\n    return load_record()\n', 'record'),
    ('api_flask.py', 'from flask import Flask\napp=Flask(__name__)\n@app.route("/record", methods=["GET"])\ndef record():\n    return load_record()\n', 'record'),
    ('api.js', 'import express from "express";\nconst app=express();\napp.get("/record", (req,res)=>{\n  return res.json(loadRecord());\n});', '@callback:3:19'),
    ('api.ts', 'import express from "express";\nconst app=express();\napp.get("/record", (req: Request,res: Response)=>{\n  return res.json(loadRecord());\n});', '@callback:3:19'),
    ('RecordApi.java', 'class RecordApi {\n @GetMapping("/record")\n Record getRecord(){ return dao.loadRecord(); }\n}', 'RecordApi.getRecord'),
])
def test_real_syntax_parsers_find_handler_and_actual_lines(name, code, symbol):
    result=describe_language(name,code)
    assert result['parse_state']=='complete'
    route=result['routes'][0]
    assert route['path']=='/record' and route['method']=='GET'
    assert route['symbol']==symbol
    reference=route['source_refs'][0]
    assert '/record' in code.splitlines()[reference['start_line']-1]
    assert any(item['name']==symbol for item in result['functions'])


@pytest.mark.parametrize('suffix',['.c','.cpp'])
def test_c_and_cpp_parse_calls_and_keep_probe_separate_from_record_query(suffix):
    source='int boot(){\n query("SELECT 1");\n return 1;\n}\nint record(){\n query("SELECT id FROM catalog_items");\n return 0;\n}\nint main(){ return boot(); }\n'
    result=describe_language('service'+suffix,source)
    assert result['parse_state']=='complete'
    assert {(row['function'],row['probe']) for row in result['data_operations']}=={('boot',True),('record',False)}
    assert result['data_operations'][1]['tables']==['catalog_items']
    assert any(row['caller']=='main' and row['callee']=='boot' for row in result['calls'])


def test_dynamic_calls_and_map_get_are_not_http_routes():
    result=describe_language('map.js','const map=new Map();\nmap.get("/record", handler);\ngetHandler()(payload);')
    assert result['routes']==[] and result['unknowns']


def test_python_startup_sql_and_http_branches_have_distinct_functions():
    source='''from fastapi import FastAPI, HTTPException
app=FastAPI()
@app.on_event("startup")
def startup():
    conn.execute("SELECT 1")
@app.get("/record")
def record():
    try:
        row=conn.execute("SELECT id FROM catalog_items WHERE id = %s", ("demo-1",))
        if not row:
            raise HTTPException(status_code=404)
        return row
    except RuntimeError:
        raise HTTPException(status_code=503)
'''
    result=describe_language(Path('api.py'),source)
    assert result['functions'][0]['entries']==['startup']
    assert [item['function'] for item in result['data_operations']]==['startup','record']
    assert result['data_operations'][1]['literal_parameters']==['demo-1']
    assert {item['status_code'] for item in result['branches'] if item['kind']=='http_status'}=={404,503}


def test_uploaded_python_is_only_parsed(tmp_path):
    marker=tmp_path/'executed'
    result=describe_language('evil.py', f'open({str(marker)!r}, "w").write("x")\n')
    assert result['parse_state']=='complete' and not marker.exists()


@pytest.mark.parametrize('sources,expected', [
    ({'app/api.py': 'import fastapi as f\nfrom .db import load as read\napp=f.FastAPI()\n@app.get("/record")\ndef record():\n return read()\n',
      'app/db.py': 'def load():\n return conn.execute("SELECT id FROM catalog_items")\n'}, 'load'),
    ({'src/api.ts': 'import ex from "express";\nimport {load as read} from "./db.js";\nconst app=ex();\napp.get("/record", handler);\nfunction handler(){ return read(); }',
      'src/db.ts': 'export function load(){ return conn.query("SELECT id FROM catalog_items"); }'}, 'load'),
    ({'src/api.js': 'const ex=require("express");\nconst db=require("./db");\nconst app=ex();\napp.get("/record", handler);\nfunction handler(){ return db.load(); }',
      'src/db.js': 'function load(){ return conn.query("SELECT id FROM catalog_items"); }'}, 'load'),
    ({'src/Api.java': 'package com.acme;\nimport com.acme.Store;\n@RequestMapping(path={"/v1","/v2"})\nclass Api {\n Store dao;\n @GetMapping(value="/record")\n Record record(){return dao.load();}\n}',
      'src/Store.java': 'package com.acme;\nclass Store {\n Record load(){return db.query("SELECT id FROM catalog_items");}\n}'}, 'Store.load'),
    ({'main.c': 'int load();\nint main(){return load();}',
      'store.c': 'int load(){query("SELECT id FROM catalog_items");return 0;}'}, 'load'),
    ({'main.cpp': 'namespace db { int load(); }\nint main(){return db::load();}',
      'store.cpp': 'namespace db { int load(){query("SELECT id FROM catalog_items");return 0;} }'}, 'db.load'),
])
def test_direct_cross_file_call_reaches_data_operation_with_original_source(sources,expected):
    files=source_files(sources)
    flows=build_flows(files,7)
    assert flows, files[0]['analysis']
    flow=flows[0]
    function=next(step for step in flow['steps'] if step['kind']=='function' and step['title']==expected)
    operation=next(step for step in flow['steps'] if step['kind']=='data_operation')
    assert operation['details']['tables']==['catalog_items']
    assert function['order']<operation['order']
    assert all(not edge['runtime_verified'] for edge in flow['edges'])
    for step in (function,operation):
        reference=step['source_refs'][0]
        original=next(row for row in files if row['id']==reference['file_id'])
        assert reference['content_digest']==original['content_digest'] and reference['profile_version']==7
        assert reference['digest_basis']=='original_bytes' and reference['original_artifact_id']=='original'
    assert not flow['control_flow_verified'] and flow['verification_status']=='static'


def test_java_class_paths_and_named_arguments_are_preserved():
    code='@RequestMapping(path={"/v1","/v2"}) class Api { @GetMapping(value="/record") Record record(){return null;} }'
    parsed=describe_language('Api.java',code)
    assert {row['path'] for row in parsed['routes']}=={'/v1/record','/v2/record'}


def test_request_and_startup_paths_keep_conditional_failure_provenance():
    source='''from fastapi import FastAPI, HTTPException
app=FastAPI()
@app.on_event("startup")
def start():
 conn.execute("SELECT 1")
@app.get("/record")
def record():
 try:
  row=conn.execute("SELECT id FROM catalog_items", ("demo-1",))
  if not row:
   raise HTTPException(status_code=404)
  return row
 except RuntimeError:
  raise HTTPException(status_code=503)
'''
    flows=build_flows(source_files({'api.py':source}),3)
    start,request=flows
    assert start['kind']=='startup' and request['kind']=='request'
    assert [step['details']['probe'] for step in start['steps'] if step['kind']=='data_operation']==[True]
    assert [step['details']['probe'] for step in request['steps'] if step['kind']=='data_operation']==[False]
    statuses={step['details']['status_code']:step for step in request['steps'] if step['details'].get('kind')=='http_status'}
    assert {arm['arm'] for arm in statuses[404]['syntax_branch_regions']}=={'true'}
    assert {arm['arm'] for arm in statuses[503]['syntax_branch_regions']}=={'except:RuntimeError'}
    assert all(not step['runtime_verified'] for step in statuses.values())


def test_ambiguous_symbols_do_not_guess_translation_unit():
    files=source_files({'main.c':'int main(){return load();}', 'one.c':'int load(){return 1;}', 'two.c':'int load(){return 2;}'})
    flow=build_flows(files,1)[0]
    assert len([step for step in flow['steps'] if step['kind']=='function'])==1
    assert any('load' in unknown for unknown in flow['unknowns'])


def test_printed_sql_is_not_assigned_a_data_operation():
    assert describe_language('app.py','print("SELECT 1")')['data_operations']==[]
    assert describe_language('app.js','console.log("SELECT 1");')['data_operations']==[]


def test_imported_python_module_alias_resolves_unique_uploaded_function():
    files=source_files({'app/api.py':'from fastapi import FastAPI\nfrom . import db\napp=FastAPI()\n@app.get("/record")\ndef record(): return db.load()\n',
                        'app/db.py':'def load(): return conn.execute("SELECT id FROM catalog_items")'})
    flow=build_flows(files,1)[0]
    assert any(step['title']=='load' and step['kind']=='function' for step in flow['steps'])


def test_candidate_route_cannot_be_confused_with_bound_framework_receiver():
    parsed=describe_language('part.py','import flask\n@app.get("/record")\ndef record(): return 1')
    assert parsed['routes'][0]['receiver_status']=='unverified_receiver' and parsed['unknowns']


def test_source_reference_version_changes_without_losing_reviewed_fact_identity():
    files=source_files({'api.py':'from fastapi import FastAPI\napp=FastAPI()\n@app.get("/record")\ndef record(): return 1'})
    files[0]['bytes']=100
    business={'id':'business','name':'test','description':'','current_version':0}
    first=build_static(business,files,None)
    route=next(fact for fact in first.facts if fact.predicate=='exposes')
    route.verification_status='confirmed'
    second=build_static({**business,'current_version':1},files,first)
    refreshed=next(fact for fact in second.facts if fact.id==route.id)
    assert refreshed.verification_status=='confirmed'
    assert refreshed.source_refs[0]['profile_version']==2
    assert first.interfaces[0]['source_refs'][0]['profile_version']==1


def test_sensitive_sql_parameters_are_not_sent_as_static_model_context():
    source='def save():\n conn.execute("INSERT INTO settings(api_key) VALUES (%s)", ("TEST_ONLY_SECRET",))\n'
    operation=describe_language('store.py',source)['data_operations'][0]
    assert operation['sensitive'] and 'TEST_ONLY_SECRET' not in str(operation)
    assert operation['literal_parameters']==['[REDACTED_PARAMETERS]']


@pytest.mark.parametrize('newline', ['', '\n'])
def test_overloaded_handlers_keep_their_own_entry_and_data_operation(newline):
    code=newline.join(['class Api {', '@GetMapping("/a") Object record(int id){return db.query("SELECT id FROM alpha");}',
                       '@GetMapping("/b") Object record(String id){return db.query("SELECT id FROM beta");}', '}'])
    flows=build_flows(source_files({'Api.java':code}),1)
    assert len(flows)==2 and len({flow['flow_id'] for flow in flows})==2
    assert {flow['entry']:[step['details']['tables'] for step in flow['steps'] if step['kind']=='data_operation']
            for flow in flows}=={'GET /a':[['alpha']], 'GET /b':[['beta']]}


def test_shadowed_express_router_parameter_is_not_a_route():
    source='import express from "express"; const app=express(); function lookUp(app){app.get("/record", fallback);} function fallback(){return "value";} lookUp(new Map());'
    parsed=describe_language('shadow.js',source)
    assert parsed['routes']==[]
    assert not build_flows(source_files({'shadow.js':source}),1)


def test_hoisted_esm_import_binds_router_declared_before_import():
    source='const app=express(); app.get("/record", handler); function handler(){return db.query("SELECT id FROM alpha");} import express from "express";'
    flows=build_flows(source_files({'hoisted.js':source}),1)
    assert len(flows)==1 and flows[0]['entry']=='GET /record'
    assert [step['details']['tables'] for step in flows[0]['steps'] if step['kind']=='data_operation']==[['alpha']]


@pytest.mark.parametrize('middle', [
    'try { throw new Map(); } catch(app){ app.get("/record",handler); }',
    'let handler=()=>db.query("SELECT id FROM alpha"); handler=()=>db.query("SELECT id FROM beta"); app.get("/record",handler);',
])
def test_shadowed_catch_and_reassigned_handler_cannot_invent_a_resolved_flow(middle):
    source='import express from "express"; const app=express(); '+middle
    if 'catch' in middle:
        source+=' function handler(){return db.query("SELECT id FROM alpha");}'
    assert not build_flows(source_files({'ambiguous.js':source}),1)
