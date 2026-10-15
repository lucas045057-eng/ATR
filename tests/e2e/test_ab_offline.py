"""A dedicated local API and local HTTP model fixture verify the native P0 UI."""
import json
import os

import httpx
import pytest
from conftest import wait_job
from playwright.sync_api import expect, sync_playwright
from test_browser import live_server  # noqa: F401
from test_business import onboard
from test_model_isolation import fake_model as loopback_model  # noqa: F401
from test_runtime import discover, target


@pytest.fixture
def fake_model(loopback_model):  # noqa: F811
    database, settings, state, received = loopback_model
    # Reach the HTTP boundary on cold Windows workers; production timeouts are unchanged.
    return database, settings.model_copy(update={'model_timeout_seconds': 15}), state, received


def test_ab_529_shows_current_facts_and_keeps_cause_unconfirmed(live_server, fake_model):  # noqa: F811
    base, directory = live_server
    _, _, model, _ = fake_model
    token = 'browser-test-operator-' + 'x' * 40
    with httpx.Client(base_url=base, headers={'Authorization': 'Bearer ' + token}) as api:
        profile = onboard(api)
        runtime = target(api, profile)
        discovery = discover(api, profile, runtime)
        binding = next(row for row in discovery['matches'] if row['kind'] == 'service')
        endpoint = '/api/v1/businesses/' + profile['business_id']
        assert api.post(endpoint + '/bindings/' + binding['id'] + '/review', json={
            'decision': 'confirm', 'reason': 'Explicit isolated target scope',
        }).status_code == 200
        model['status'] = 529
        before = model['requests']
        symptom = 'P0 isolated outage ' + profile['business_id'][:8]
        event = api.post('/api/v1/incidents', json={
            'system_id': profile['system_id'], 'symptom': symptom,
            'task_type': 'root_cause_investigation',
        }).json()
        started = api.post('/api/v1/incidents/' + event['id'] + '/runs', json={
            'target_id': runtime['id'], 'decision_protocol': 'ab_v1',
        }).json()
        job = wait_job(api, started['job_id'], timeout=30)
        assert job['status'] == 'completed', job
        run = api.get('/api/v1/runs/' + started['run_id']).json()
        assert model['requests'] - before == 1
        assert run['summary']['model_error']['status_code'] == 529
        assert len(run['tool_calls']) >= 2 and not run['repair_eligible']
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=os.environ.get('ATR_BROWSER_CHANNEL') or None)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ')')
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base)
        page.get_by_role('button', name='排查故障', exact=False).click()
        page.locator('.incident-list').get_by_text(symptom, exact=True).click()
        page.get_by_test_id('evidence-audit').locator('summary').first.click()
        expect(page.get_by_role('heading', name='只读故障事实报告', exact=True)).to_be_visible(timeout=10000)
        panel = page.get_by_test_id('ab-fact-report')
        expect(panel).to_contain_text('已确认事实')
        expect(panel).to_contain_text('直接异常')
        expect(panel).to_contain_text('未知原因')
        expect(panel).to_contain_text('缺失证据')
        expect(panel).to_contain_text('failed')
        expect(page.locator('.diagnosis-summary')).to_contain_text('当前独立事实')
        expect(page.get_by_test_id('model-error')).to_contain_text('HTTP 529')
        assert 'synthetic-secret' not in page.locator('body').inner_text()
        assert model['requests'] - before == 1
        assert not errors
        page.screenshot(path=str(directory / 'p0-ab-offline.png'), full_page=True)
        browser.close()
