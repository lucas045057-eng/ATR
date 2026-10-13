import ast
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest


def probe(status=503,**changes):
    module=runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts'/'gateway_upgrade.py'))
    function=next(node for node in ast.parse(module['REMOTE']).body if isinstance(node,ast.FunctionDef) and node.name=='probe')
    endpoint={'endpoint_id':'entry','url':'http://127.0.0.1:18000/health'}
    def read(argv,**kwargs):
        request=json.loads(kwargs['input'])
        value={'schema_version':1} if request['tool_id']=='capabilities' else {
            'protocol':'atr-read-ab-v1','tool_version':1,'observation_location':'remote_ssh',
            'observed_host':'business-server','endpoint_id':'entry','url':endpoint['url'],
            'status_code':status,**changes}
        return SimpleNamespace(stdout=json.dumps(value).encode())
    namespace={'json':json,'subprocess':SimpleNamespace(run=read),
               'policy':SimpleNamespace(read_text=lambda:json.dumps({'health_endpoints':[endpoint]}))}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'gateway-maintenance-probe','exec'),namespace)
    return namespace['probe']('/owned/gateway.py')


def test_maintenance_probe_accepts_valid_unhealthy_business_response():
    assert probe(503)[-1]['status_code']==503


@pytest.mark.parametrize('changes',[{'endpoint_id':'other'}, {'url':'http://other/health'},
                                    {'observation_location':'local'}, {'status_code':True}, {'status_code':999}])
def test_maintenance_probe_rejects_invalid_identity_or_status(changes):
    with pytest.raises(AssertionError):
        probe(200,**changes)
