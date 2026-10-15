"""Real browser, local API and local fake provider: failure then recovery."""
import json
import os

import httpx
import pytest
from conftest import wait_job
from playwright.sync_api import expect, sync_playwright
from test_browser import (
    legacy_run_requests,
    live_server,  # noqa: F401 - pytest fixture
)
from test_business import onboard
from test_model_isolation import fake_model  # noqa: F401 - pytest fixture
from test_runtime import discover, target
from test_v22_adaptive import hypothesis, report


@pytest.fixture
def low_input_budget(monkeypatch):
    monkeypatch.setenv('INVESTIGATION_INPUT_TOKENS', '1000')


@pytest.mark.parametrize('status,message', [(401, '模型供应商认证失败'), (400, '模型供应商报告模型价格未配置')])
def test_browser_model_failure_is_visible_and_next_request_recovers(live_server, fake_model, status, message):  # noqa: F811 - imported pytest fixtures
    base, directory = live_server
    _, _, state, _ = fake_model
    token = "browser-test-operator-" + "x" * 40
    state["status"] = status
    if status == 400:
        state['error'] = {'type': 'invalid_request_error', 'message': 'Model gpt-6.1-sol has not been priced by the administrator yet. synthetic-secret NEVER-ECHO-REQUEST'}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=os.environ.get("ATR_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ")")
        page = context.new_page()
        errors, statuses = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("response", lambda r: statuses.append(r.status))
        page.goto(base)
        page.get_by_role("button", name="导入业务资料", exact=False).click()
        page.get_by_label("业务名称", exact=True).fill("模型失败恢复 Mock")
        page.get_by_role("button", name="创建并保存", exact=True).click()
        analyze = page.get_by_role("button", name="进行模型理解", exact=True)
        expect(analyze).to_be_enabled(timeout=10000)
        analyze.click()
        expect(page.get_by_text("模型调用失败", exact=True)).to_be_visible(timeout=20000)
        expect(page.get_by_role("alert").filter(has_text="错误类型：")).to_contain_text(message)
        expect(page.get_by_text("后台任务：failed", exact=True)).to_be_visible()
        expect(analyze).to_be_enabled()
        page.screenshot(path=str(directory / "model-failure.png"), full_page=True)
        assert "synthetic-secret" not in page.locator("body").inner_text()
        assert "NEVER-ECHO-REQUEST" not in page.locator("body").inner_text()
        state["status"] = 200
        analyze.click()
        expect(page.get_by_text("后台任务：completed", exact=True)).to_be_visible(timeout=20000)
        expect(page.get_by_role("alert")).to_have_count(0)
        assert httpx.get(base + "/api/v1/health").status_code == 200
        assert errors == [] and 504 not in statuses
        assert state["requests"] == 2
        browser.close()


def test_browser_usage_is_read_only_despite_small_legacy_budget(low_input_budget, live_server, fake_model):  # noqa: F811
    base, _ = live_server
    _, _, state, _ = fake_model
    token = 'browser-test-operator-' + 'x' * 40
    with httpx.Client(base_url=base, headers={'Authorization': 'Bearer ' + token}) as api:
        profile = onboard(api)
        runtime = target(api, profile)
        discovered = discover(api, profile, runtime)
        binding = next(b for b in discovered['matches'] if b['kind'] == 'service')
        path = '/api/v1/businesses/' + profile['business_id']
        api.post(path + '/bindings/' + binding['id'] + '/review',
                 json={'decision': 'confirm', 'reason': '隔离授权核验'}).raise_for_status()
        profile = api.get(path).json()
        api.post(path + '/review', json={'expected_version': profile['profile_version'],
                 'decision': 'approve', 'reason': '隔离资料核验'}).raise_for_status()
        symptom = '预算预检查验收 ' + profile['business_id'][:8]
        incident = api.post('/api/v1/incidents', json={'system_id': profile['system_id'],
                            'symptom': symptom, 'mode': 'graph_adaptive'}).json()
        task = api.post('/api/v1/incidents/' + incident['id'] + '/runs', json={'decision_protocol': 'legacy_v22'}).json()
        job = wait_job(api, task['job_id'])
        result = api.get('/api/v1/runs/' + job['result']['id']).json()
        assert result['context_budget']['mode'] == 'accounting_only'
        assert result['context_budget']['stop_reason'] is None
        assert result['context_budget']['calls'] == state['requests'] > 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=os.environ.get('ATR_BROWSER_CHANNEL') or None)
        context = browser.new_context()
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ")")
        page = context.new_page()
        page.goto(base)
        page.get_by_role('button', name='排查故障', exact=False).click()
        page.locator('.incident-list').get_by_text(symptom, exact=True).click()
        page.get_by_test_id('evidence-audit').locator('summary').first.click()
        expect(page.get_by_text('预算只读，不限制调用', exact=False)).to_be_visible()
        expect(page.get_by_test_id('budget-error-details')).to_have_count(0)
        browser.close()


def test_browser_529_investigation_history_and_manual_recovery(live_server, fake_model):  # noqa: F811
    base, directory = live_server
    _, _, state, _ = fake_model
    token = "browser-test-operator-" + "x" * 40
    with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token}) as api:
        profile = onboard(api)
        runtime = target(api, profile)
        discovered = discover(api, profile, runtime)
        binding = next(b for b in discovered["matches"] if b["kind"] == "service")
        path = '/api/v1/businesses/' + profile['business_id']
        assert api.post(path + '/bindings/' + binding['id'] + '/review', json={"decision": "confirm", "reason": "隔离 Mock 授权范围"}).status_code == 200
        profile = api.get(path).json()
        assert api.post(path + '/review', json={"expected_version": profile['profile_version'], "decision": "approve", "reason": "隔离资料人工核验"}).status_code == 200
        state['status'] = 529
        symptom = 'catalog-api startup failure ' + profile['business_id'][:8]
        incident = api.post('/api/v1/incidents', json={"system_id": profile['system_id'], "symptom": symptom, "mode": "graph_adaptive"}).json()
        task = api.post('/api/v1/incidents/' + incident['id'] + '/runs', json={'decision_protocol': 'legacy_v22'}).json()
        job = wait_job(api, task['job_id'], timeout=20)
        result = api.get('/api/v1/runs/' + job['result']['id']).json()
        assert result['summary']['model_error']['status_code'] == 529
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=os.environ.get("ATR_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ")")
        page = context.new_page()
        legacy_run_requests(page)
        errors, responses = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('response', lambda r: responses.append(r.url))
        page.goto(base)
        page.get_by_role('button', name='排查故障', exact=False).click()
        page.locator('.incident-list').get_by_text(symptom, exact=True).click()
        page.get_by_test_id('evidence-audit').locator('summary').first.click()
        panel = page.get_by_test_id('model-error')
        expect(panel).to_contain_text('模型服务暂时不可用', timeout=10000)
        expect(panel).to_contain_text('HTTP 529')
        expect(panel).to_contain_text('APIError')
        expect(panel).to_contain_text('未自动重试')
        expect(panel).to_contain_text('可能产生额外费用')
        expect(page.get_by_text('Agent 阶段：', exact=False)).to_contain_text('已停止')
        page.screenshot(path=str(directory / 'investigation-529.png'), full_page=True)
        assert 'synthetic-secret' not in page.locator('body').inner_text()
        assert 'NEVER-ECHO-REQUEST' not in page.locator('body').inner_text()
        state['status'] = 200
        state['reply'] = lambda c, _: report([hypothesis(c, 'catalog-api', failure='startup_failure', all_evidence=True)])
        page.get_by_role('button', name='开始只读调查', exact=False).click()
        expect(panel).to_have_count(0, timeout=20000)
        page.get_by_test_id('evidence-audit').locator('summary').first.click()
        expect(page.locator('.run-status-line strong')).to_have_text('证据核验通过', timeout=20000)
        latest = httpx.get(base + '/api/v1/incidents/' + incident['id'] + '/runs',
                           headers={'Authorization': 'Bearer ' + token}).json()
        assert len(latest) == 2
        recovered = httpx.get(base + '/api/v1/runs/' + latest[0]['id'],
                              headers={'Authorization': 'Bearer ' + token}).json()
        assert recovered['summary']['gate']['status'] == 'supported'
        assert recovered['summary']['gate']['hypotheses'][0]['verification']['passed'] is True
        expect(page.get_by_role('button', name='开始只读调查', exact=False)).to_be_enabled(timeout=10000)
        # Allow the final one-shot refresh to finish before checking recurring polls.
        page.wait_for_timeout(500)
        count = len([r for r in responses if '/jobs/' in r or '/runs/' in r])
        page.wait_for_timeout(2200)
        assert len([r for r in responses if '/jobs/' in r or '/runs/' in r]) == count
        assert state['requests'] == 2 and errors == []
        browser.close()


@pytest.mark.parametrize('invalid_reference', [False, True, 'prediction', 'counter'])
def test_browser_schema_error_shows_safe_field_path(live_server, fake_model, invalid_reference):  # noqa: F811
    base, directory = live_server
    _, _, state, _ = fake_model
    token = "browser-test-operator-" + "x" * 40
    with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token}) as api:
        profile = onboard(api)
        runtime = target(api, profile)
        discovered = discover(api, profile, runtime)
        binding = next(b for b in discovered['matches'] if b['kind'] == 'service')
        path = '/api/v1/businesses/' + profile['business_id']
        assert api.post(path + '/bindings/' + binding['id'] + '/review', json={'decision': 'confirm', 'reason': '隔离授权核验'}).status_code == 200
        profile = api.get(path).json()
        assert api.post(path + '/review', json={'expected_version': profile['profile_version'], 'decision': 'approve', 'reason': '隔离资料核验'}).status_code == 200

        def reply(c, _):
            h = hypothesis(c, 'catalog-api')
            if invalid_reference == 'counter':
                h = hypothesis(c, 'store')
                h['evidence_links'] = [{'evidence_id': c['current_evidence'][0]['evidence_id'],
                                       'relation': 'refutes', 'explanation': 'Application state is not datastore counterevidence.'}]
                return report([h])
            if invalid_reference == 'prediction':
                h['verification_predictions'][0]['target_node_id'] = 'PRIVATE-INVALID-PREDICTION'
                return report([h])
            if invalid_reference:
                h['target_node_id'] = 'PRIVATE-INVALID-TARGET'
                return report([h])
            h['confidence'] = 'PRIVATE-OUTPUT-VALUE'
            return report([h], **{'PRIVATE-FIELD-KEY': 'PRIVATE-OUTPUT-VALUE'})

        state['reply'] = reply
        symptom = '安全字段诊断 ' + profile['business_id'][:8]
        incident = api.post('/api/v1/incidents', json={'system_id': profile['system_id'], 'symptom': symptom, 'mode': 'graph_adaptive'}).json()
        task = api.post('/api/v1/incidents/' + incident['id'] + '/runs', json={'decision_protocol': 'legacy_v22'}).json()
        job = wait_job(api, task['job_id'], timeout=20)
        result = api.get('/api/v1/runs/' + job['result']['id']).json()
        if invalid_reference == 'counter':
            assert result['summary']['model_error'] is None
            assert result['summary']['decision_audit']['rejected_relation_count'] == 1
            assert result['summary']['gate']['status'] != 'supported'
        else:
            assert result['summary']['model_error']['validation_kind'] == ('decision' if invalid_reference else 'format')
        assert len(result['tool_calls']) == 2
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=os.environ.get('ATR_BROWSER_CHANNEL') or None)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ")")
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(base)
        page.get_by_role('button', name='排查故障', exact=False).click()
        page.locator('.incident-list').get_by_text(symptom, exact=True).click()
        page.get_by_test_id('evidence-audit').locator('summary').first.click()
        panel = page.get_by_test_id('model-error')
        if invalid_reference == 'counter':
            warning = page.get_by_text('局部证据关系已隔离', exact=False)
            expect(warning).to_be_visible(timeout=10000)
            expect(warning).to_contain_text('hypotheses[0].evidence_links[0].relation')
            expect(warning).to_contain_text('counterevidence_target_mismatch')
        elif invalid_reference == 'prediction':
            expect(panel).to_contain_text('hypotheses[0].verification_predictions[0].target_node_id', timeout=10000)
            expect(panel).to_contain_text('unknown_prediction_target')
            expect(panel).to_contain_text('预测目标不在本轮允许引用的服务节点集合中')
        elif invalid_reference:
            expect(panel).to_contain_text('hypotheses[0].target_node_id', timeout=10000)
            expect(panel).to_contain_text('unknown_target')
            expect(panel).to_contain_text('不在本轮允许引用的服务节点集合中')
        else:
            expect(panel).to_contain_text('hypotheses[0].confidence', timeout=10000)
            expect(panel).to_contain_text('float_type')
            expect(panel).to_contain_text('格式校验失败')
            expect(panel).to_contain_text('<extra_field>')
        assert 'PRIVATE-' not in page.locator('body').inner_text()
        assert 'synthetic-secret' not in page.locator('body').inner_text()
        assert errors == [] and state['requests'] == 1
        page.screenshot(path=str(directory / f'schema-fields-{invalid_reference}.png'), full_page=True)
        browser.close()


def test_browser_discovery_next_tools_error_is_safe_and_specific(live_server, fake_model):  # noqa: F811
    from test_autonomous_discovery import mapping

    base, _ = live_server
    _, _, provider, _ = fake_model
    token = "browser-test-operator-" + "x" * 40
    with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token}) as api:
        profile = onboard(api)
        target(api, profile)
        path = '/api/v1/businesses/' + profile['business_id']
        profile = api.post(path + '/review', json={"expected_version": profile['profile_version'],
            "decision": "approve", "reason": "Only isolated discovery, no confirmed mappings"}).json()

        def invalid(context, _):
            data = mapping(context)
            data['proposals'][0]['next_tools'] = ['PRIVATE-UNAUTHORIZED-TOOL']
            return data

        provider['reply'] = invalid
        symptom = '候选工具契约验收 ' + profile['business_id'][:8]
        incident = api.post('/api/v1/incidents', json={'system_id': profile['system_id'], 'symptom': symptom, 'mode': 'normal'}).json()
        task = api.post('/api/v1/incidents/' + incident['id'] + '/runs').json()
        job = wait_job(api, task['job_id'])
        result = api.get('/api/v1/runs/' + job['result']['run_id']).json()
        assert len(result['tool_calls']) == 6  # Docker is explicitly disabled in this fixture.
        assert result['summary']['mapping_proposals'] == []
        assert result['summary']['model_error']['validation_errors'][0]['type'] == 'action_not_allowed'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=os.environ.get('ATR_BROWSER_CHANNEL') or None)
        context = browser.new_context()
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ")")
        page = context.new_page()
        page.goto(base)
        page.get_by_role('button', name='排查故障', exact=False).click()
        page.locator('.incident-list').get_by_text(symptom, exact=True).click()
        panel = page.get_by_test_id('model-error')
        expect(panel).to_contain_text('proposals[0].next_tools')
        expect(panel).to_contain_text('action_not_allowed')
        expect(panel).to_contain_text('动作不在本轮授权候选中')
        assert 'PRIVATE-' not in page.locator('body').inner_text()
        assert provider['requests'] == 1
        browser.close()
