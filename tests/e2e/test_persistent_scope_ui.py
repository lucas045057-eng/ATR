"""Native local API + Mock objects + loopback model: daily one-submit workflow."""
import json
import os

import httpx
from playwright.sync_api import expect, sync_playwright
from test_ab_offline import fake_model  # noqa: F401
from test_browser import live_server  # noqa: F401
from test_business import onboard
from test_model_isolation import fake_model as loopback_model  # noqa: F401
from test_runtime import discover, target


def test_revoke_authorization_without_written_reason(live_server):  # noqa: F811
    base, _ = live_server
    token = 'browser-test-operator-' + 'x' * 40
    with httpx.Client(base_url=base, headers={'Authorization': 'Bearer ' + token}) as api:
        profile = onboard(api)
        runtime = target(api, profile)
        binding = next(row for row in discover(api, profile, runtime)['matches'] if row['kind'] == 'service')
        endpoint = f"/api/v1/businesses/{profile['business_id']}"
        api.post(endpoint + '/bindings/' + binding['id'] + '/review', json={'decision': 'confirm'}).raise_for_status()
        binding = next(row for row in api.get(endpoint + '/bindings').json() if row['id'] == binding['id'])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=os.environ.get('ATR_BROWSER_CHANNEL') or None)
        context = browser.new_context()
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ')')
        page = context.new_page()
        dialogs = []
        page.on('dialog', lambda dialog: (dialogs.append(dialog.type), dialog.accept()))
        page.goto(base)
        page.get_by_role('button', name='服务器与服务', exact=False).click()
        row = page.get_by_test_id('binding-authorization').filter(has_text=binding['runtime_id'])
        with page.expect_response(lambda response: response.url.endswith('/revoke')) as revoked:
            row.get_by_role('button', name='撤销授权', exact=True).click()
        assert revoked.value.status == 200
        assert dialogs == ['confirm']
        expect(row).to_contain_text('持久授权：已撤销')
        browser.close()


def test_authorized_fault_submit_starts_native_run(live_server, fake_model):  # noqa: F811
    assert not os.environ.get('ATR_E2E_BASE_URL'), 'Use dedicated local fixture only'
    base, directory = live_server
    assert '127.0.0.1:' in base and ':28080' not in base
    _, settings, model, _ = fake_model
    assert '127.0.0.1' in settings.model_base_url
    token = 'browser-test-operator-' + 'x' * 40
    with httpx.Client(base_url=base, headers={'Authorization': 'Bearer ' + token}) as api:
        profile = onboard(api)
        runtime = target(api, profile)
        discovery = discover(api, profile, runtime)
        binding = next(row for row in discovery['matches'] if row['kind'] == 'service')
        endpoint = '/api/v1/businesses/' + profile['business_id']
        approved = api.post(endpoint + '/bindings/' + binding['id'] + '/review', json={
            'decision': 'confirm', 'reason': 'Explicit isolated daily scope',
            'identity_mode': 'exact_instance',
        })
        assert approved.status_code == 200, approved.text
        binding = next(row for row in api.get(endpoint + '/bindings').json() if row['id'] == binding['id'])
        model['status'] = 529
        before = model['requests']
    symptom = 'Catalog isolated daily outage'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=os.environ.get('ATR_BROWSER_CHANNEL') or None)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ')')
        page = context.new_page()
        errors, starts = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: starts.append(request.post_data_json)
                if request.method == 'POST' and '/incidents/' in request.url and request.url.endswith('/runs') else None)
        page.goto(base)
        page.get_by_role('button', name='服务器与服务', exact=False).click()
        page.get_by_label('运行环境', exact=True).select_option(runtime['id'])
        authorized = page.get_by_test_id('binding-authorization').filter(has_text=binding['runtime_id'])
        expect(authorized).to_contain_text('持久授权：有效')
        expect(authorized).to_contain_text('精确实例')
        expect(authorized.get_by_role('checkbox')).not_to_be_checked()
        expect(authorized.get_by_role('button', name='明确升级为稳定对象授权')).to_be_disabled()
        authorized.get_by_role('checkbox').check()
        expect(authorized.locator('input:not([type="checkbox"])')).to_have_count(0)
        authorized.get_by_role('button', name='明确升级为稳定对象授权').click()
        expect(authorized).to_contain_text('稳定业务对象')
        authorized.locator('summary').filter(has_text='授权审核历史').click()
        expect(authorized).to_contain_text('升级身份规则')
        page.locator('summary').filter(has_text='日常调查默认设置').click()
        page.get_by_label('默认服务范围', exact=True).select_option(binding['service'])
        page.get_by_label('优先调查服务', exact=True).select_option(binding['service'])
        with page.expect_response(lambda response: response.request.method == 'PUT' and response.url.endswith('/investigation-defaults')) as saved_response:
            page.get_by_role('button', name='将当前目标保存为调查默认', exact=True).click()
        assert saved_response.value.status == 200
        with httpx.Client(base_url=base, headers={'Authorization': 'Bearer ' + token}) as api:
            saved = api.get(endpoint + '/investigation-defaults').json()
            assert saved['target_id'] == runtime['id']
            assert saved['services'] == [binding['service']] and saved['primary_service'] == binding['service']
        page.get_by_role('button', name='排查故障', exact=False).click()
        page.locator('select[name="system_id"]').select_option(profile['system_id'])
        page.locator('textarea[name="symptom"]').fill(symptom)
        expect(page.locator('select[name="task_type"]')).to_have_value('focused_diagnosis')
        page.locator('.incident-panel form').get_by_role('button', name='提交故障并开始调查', exact=False).click()
        expect(page.get_by_test_id('investigation-conclusion')).to_contain_text('直接故障定位目标已完成', timeout=30000)
        page.get_by_test_id('evidence-audit').locator('summary').first.click()
        expect(page.get_by_role('heading', name='只读故障事实报告', exact=True)).to_be_visible(timeout=30000)
        expect(page.get_by_test_id('ab-fact-report')).to_contain_text('已确认事实')
        expect(page.get_by_test_id('model-error')).to_have_count(0)
        expect(page.locator('.repair-form')).to_have_count(0)
        expect(page.get_by_role('button', name='确认所选对象并继续')).to_have_count(0)
        expect(page.get_by_test_id('unknown-object-review')).to_contain_text('未知对象待独立审批，不阻断当前授权调查')
        expect(page.locator('.run-view')).to_contain_text('本轮运行身份核验')
        assert len(starts) == 1 and starts[0]['decision_protocol'] == 'ab_v1'
        assert starts[0]['target_id'] == runtime['id']
        assert model['requests'] - before == 0
        assert not errors
        page.screenshot(path=str(directory / 'p05-one-submit.png'), full_page=True)
        page.get_by_role('button', name='到服务器与服务独立审批').click()
        expect(page.get_by_label('选择业务', exact=True)).to_have_value(profile['business_id'])
        expect(page.get_by_test_id('binding-authorization').filter(has_text=binding['runtime_id'])).to_contain_text('持久授权：有效')
        page.get_by_text('未匹配或人工判定不相关（默认折叠）', exact=True).first.click()
        unknown = page.get_by_test_id('unknown-object-approval').filter(has_text='unrelated-backup')
        unknown.locator('summary').click()
        unknown.get_by_label('本业务中的名称', exact=True).fill('catalog-backup')
        expect(unknown.locator('input[name="reason"]')).to_have_count(0)
        unknown.get_by_label('我确认此对象属于当前业务，并授权只读检查。', exact=True).check()
        with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/authorize-object')) as approved_response:
            unknown.get_by_role('button', name='确认此对象授权', exact=True).click()
        assert approved_response.value.status == 200
        with httpx.Client(base_url=base, headers={'Authorization': 'Bearer ' + token}) as api:
            rows = api.get(endpoint + '/bindings').json()
            assert any(row['service'] == 'catalog-backup' and row['authorization_valid'] for row in rows)
        assert len(starts) == 1 and model['requests'] - before == 0
        assert not errors
        browser.close()


def test_ambiguous_business_names_do_not_create_or_start(live_server):  # noqa: F811
    assert not os.environ.get('ATR_E2E_BASE_URL'), 'Use dedicated local fixture only'
    base, directory = live_server
    assert '127.0.0.1:' in base and ':28080' not in base
    token = 'browser-test-operator-' + 'x' * 40
    with httpx.Client(base_url=base, headers={'Authorization': 'Bearer ' + token}) as api:
        first = onboard(api)
        second = onboard(api)
        assert first['system_id'] != second['system_id']
        before = len(api.get('/api/v1/incidents').json())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=os.environ.get('ATR_BROWSER_CHANNEL') or None)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ')')
        page = context.new_page()
        page.goto(base)
        page.get_by_role('button', name='排查故障', exact=False).click()
        page.locator('select[name="system_id"]').select_option(first['system_id'])
        page.locator('textarea[name="symptom"]').fill('Catalog outage with ambiguous registered business name')
        page.locator('.incident-panel form').get_by_role('button', name='提交故障并开始调查', exact=False).click()
        expect(page.locator('.alert.error')).to_contain_text('故障描述提到了多个业务')
        with httpx.Client(base_url=base, headers={'Authorization': 'Bearer ' + token}) as api:
            assert len(api.get('/api/v1/incidents').json()) == before
            assert api.get('/api/v1/runs').json() == []
        page.screenshot(path=str(directory / 'p05-ambiguous-business.png'), full_page=True)
        browser.close()
