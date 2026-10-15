"""Real built UI with controlled API contracts; no real SSH/model acceptance claims."""

import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def ui_origin():
    handler = partial(SimpleHTTPRequestHandler, directory=str(ROOT / "frontend/dist"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def ui(ui_origin):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, channel=os.environ.get("ATR_BROWSER_CHANNEL") or None,
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page, ui_origin
        assert not errors
        browser.close()


def contract(page, *, name="orders-api", diagnosis="direct_cause_supported", saved=False):
    result = {
        "investigation_goal": "direct_fault", "diagnosis_status": diagnosis,
        "completion_satisfied": diagnosis == "direct_cause_supported",
        "direct_findings": [{"service": name, "kind": "service_state", "value": "stopped",
                             "evidence_id": "evidence-state", "observed_at": "2026-10-15T08:00:00Z"}],
        "direct_explanations": [{"symptom": "API 未响应", "explanation": f"{name} 已停止，无法提供服务",
                                 "evidence_ids": ["evidence-state", "evidence-entry"]}],
        "trigger_cause_status": "unknown",
        "impact_scope": {"services": [name], "entry_impact_verified": True,
                         "description": f"已验证入口仅由 {name} 承载"},
        "required_gaps": [], "model_unknowns": [],
    }
    run = {"id": "run-original", "incident_id": "incident-original", "system_id": "system-1",
           "status": "supported" if diagnosis == "direct_cause_supported" else "unresolved",
           "execution_status": "completed", "diagnosis_status": diagnosis,
           "diagnosis_result": result, "completion_satisfied": result["completion_satisfied"],
           "summary": {"summary": f"已确认 {name} 停止", "decision_protocol": "ab_v1"},
           "evidence": [{"id": "evidence-state", "tool_id": "service.status", "freshness_state": "current",
                         "content_hash": "123456789abcdef", "payload": {"text": "raw audit evidence"}}]}
    state = {"run": run, "posts": [], "gets": [], "failure": None, "job_status": "completed"}

    def route(request_route):
        request = request_route.request
        path = urlsplit(request.url).path.removeprefix("/api/v1")
        if request.method == "GET":
            state["gets"].append(path)
        else:
            state["posts"].append((path, request.post_data_json))
        if path in {"/jobs/job-original", "/runs/run-original"} and state["failure"]:
            failure = state["failure"]
            if failure == "network":
                request_route.abort("internetdisconnected")
            elif failure == "hang":
                return
            else:
                request_route.fulfill(status=failure, json={"detail": "Read unavailable"})
            return
        responses = {
            "/systems": [{"id": "system-1", "name": "Isolated business", "source_path": "business://business-1"}],
            "/businesses": [{"id": "business-1", "system_id": "system-1", "name": "Isolated business"}],
            "/businesses/business-1": None,
            "/businesses/business-1/targets": [{"id": "target-1", "name": "Local fixture", "host": "127.0.0.1",
                                               "investigation_readiness": {"eligible": True}}],
            "/businesses/business-1/investigation-defaults": {"target_id": "target-1"},
            "/systems/system-1/graph": {"nodes": [], "edges": [], "conflicts": []},
            "/incidents": [{"id": "incident-original", "system_id": "system-1", "status": "open",
                            "symptom": {"text": "API 未响应"}, "latest_run_id": "run-original"}],
            "/runs/run-original": state["run"],
            "/jobs/job-original": {"status": state["job_status"], "result": {"run_id": "run-original"}},
            "/metrics": {}, "/model/capabilities": {},
        }
        if request.method == "POST" and path == "/incidents":
            body = {"id": "incident-original"}
        elif request.method == "POST" and path.endswith("/runs"):
            body = {"job_id": "job-original", "run_id": "run-original"}
        else:
            body = responses.get(path, [])
        request_route.fulfill(body=json.dumps(body), content_type="application/json")

    page.route("**/api/v1/**", route)
    if saved:
        # Only seed once so reloads exercise storage written by the application.
        page.add_init_script("""if (!sessionStorage.getItem('fixture_seeded')) {
            sessionStorage.setItem('fixture_seeded', 'yes');
            sessionStorage.setItem('atr_active_run_job', 'job-original');
            sessionStorage.setItem('atr_active_run_id', 'run-original');
        }""")
    return state


def open_investigation(page, origin):
    page.goto(origin)
    page.get_by_role("button", name="排查故障", exact=False).click()


@pytest.mark.parametrize("deep", [False, True])
def test_task_defaults_to_direct_and_allows_explicit_deep_goal(ui, deep):
    page, origin = ui
    state = contract(page)
    open_investigation(page, origin)
    expect(page.locator('select[name="task_type"]')).to_have_value("focused_diagnosis")
    if deep:
        page.locator('select[name="task_type"]').select_option("root_cause_investigation")
    page.get_by_label("故障现象", exact=True).fill("API 未响应")
    page.get_by_role("button", name="提交故障并开始调查", exact=False).click()
    expect(page.locator(".run-view")).to_be_visible()
    created = [body for path, body in state["posts"] if path == "/incidents"]
    if deep:
        assert created[0]['task_type'] == 'root_cause_investigation'
    else:
        # Leaving the default alone must preserve the backend's symptom intent inference.
        assert 'task_type' not in created[0]
    assert len([path for path, _ in state["posts"] if path.endswith("/runs")]) == 1
    assert [body for path, body in state["posts"] if path.endswith("/runs")][0]["decision_protocol"] == "ab_v1"


@pytest.mark.parametrize("name", ["orders-api", "catalog-worker"])
def test_direct_result_leads_with_scope_evidence_unknowns_and_no_repair_qualification(ui, name, tmp_path):
    page, origin = ui
    contract(page, name=name)
    open_investigation(page, origin)
    page.locator(".incident-list").get_by_text("API 未响应", exact=True).click()
    conclusion = page.get_by_test_id("investigation-conclusion")
    expect(conclusion).to_contain_text("直接原因已定位")
    expect(conclusion).to_contain_text(name)
    expect(conclusion).to_contain_text("API 未响应")
    expect(conclusion).to_contain_text("evidence-state")
    expect(conclusion).to_contain_text("触发原因尚未确认")
    expect(conclusion).to_contain_text("直接故障定位目标已完成")
    expect(conclusion).to_contain_text("本次目标：直接故障定位")
    expect(conclusion).to_contain_text("业务恢复尚未验证")
    expect(page.locator(".repair-form")).to_have_count(0)
    audit = page.get_by_test_id("evidence-audit")
    expect(audit).not_to_have_attribute("open", "")
    expect(page.get_by_text("raw audit evidence", exact=True)).not_to_be_visible()
    if name == "catalog-worker":
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / f"{name}-conclusion.png"), full_page=True)
    audit.locator("summary").first.click()
    expect(page.get_by_text("raw audit evidence", exact=True)).to_be_visible()


def test_single_replica_fact_does_not_claim_business_outage_or_goal_completion(ui):
    page, origin = ui
    state = contract(page, diagnosis="partial")
    state["run"]["diagnosis_result"].update(
        direct_explanations=[],
        impact_scope={"services": ["orders-api"], "entry_impact_verified": False,
                      "description": "仅 orders-api 的一个副本停止；入口仍正常"},
    )
    open_investigation(page, origin)
    page.locator(".incident-list").get_by_text("API 未响应", exact=True).click()
    conclusion = page.get_by_test_id("investigation-conclusion")
    expect(conclusion).to_contain_text("一个副本停止；入口仍正常")
    expect(conclusion).to_contain_text("本次目标尚未完成")
    expect(conclusion).not_to_contain_text("直接原因已定位")
    expect(page.locator(".repair-form")).to_have_count(0)


@pytest.mark.parametrize("failure", ["network", 401, 404, "hang"])
def test_poll_failure_unlocks_new_investigation_and_recovers_same_run_without_posts(ui, failure):
    page, origin = ui
    state = contract(page, saved=True)
    state["failure"] = failure
    state["run"]["execution_status"] = "running"
    state["job_status"] = "running"
    page.clock.install()
    open_investigation(page, origin)
    expect(page.get_by_label("目标系统")).to_contain_text("Isolated business")
    if failure == "hang":
        page.wait_for_function("document.readyState === 'complete'")
        assert "/runs/run-original" in state["gets"] or "/jobs/job-original" in state["gets"]
        page.clock.fast_forward(31_000)
    connection = page.get_by_test_id("run-connection")
    expect(connection).to_contain_text("调查状态暂时未知", timeout=5000)
    expect(page.get_by_role("button", name="提交故障并开始调查", exact=False)).to_be_enabled()
    expect(page.get_by_role("button", name="调查进行中", exact=False)).to_have_count(0)
    page.reload()
    page.get_by_role("button", name="排查故障", exact=False).click()
    if failure == "hang":
        page.clock.fast_forward(31_000)
    expect(connection).to_contain_text("调查状态暂时未知", timeout=5000)
    state["failure"] = None
    state["job_status"] = "completed"
    state["run"]["execution_status"] = "completed"
    page.get_by_role("button", name="重新读取同一调查", exact=True).click()
    expect(page.locator(".run-view")).to_contain_text("执行状态：已结束")
    assert state["posts"] == []
    assert "/runs/run-original" in state["gets"]


@pytest.mark.parametrize("entry_verified", [False, True])
def test_actual_fact_report_contract_preserves_instance_and_endpoint_boundaries(ui, entry_verified):
    page, origin = ui
    state = contract(page)
    result = state["run"]["diagnosis_result"]
    report = {
        "summary": "已确认 catalog-worker 停止；该实例无法提供服务。",
        "investigation_goal": "direct_fault", "trigger_cause_status": "unknown",
        "direct_anomalies": result["direct_findings"],
        "direct_explanations": [{"target_node_id": "node-worker", "kind": "instance_unavailable",
                                 "symptom": "API 未响应", "scope": "instance",
                                 "evidence_ids": ["evidence-state"], "trigger_cause_status": "unknown"}],
        "impact_scope": {"level": "instance", "target_node_ids": ["node-worker"],
                         "entry_impact_verified": entry_verified,
                         "endpoints": [{"endpoint_id": "entry-api", "url": "https://isolated.invalid/api"}]
                         if entry_verified else []},
        "confirmed_facts": [], "unknown_causes": [], "missing_evidence": [], "checks": [],
    }
    state["run"]["summary"] = {"investigation_goal": "direct_fault", "report": {"fact_report": report}}
    state["run"]["diagnosis_result"] = {"diagnosis_status": "direct_cause_supported", "completion_satisfied": True}
    open_investigation(page, origin)
    page.locator(".incident-list").get_by_text("API 未响应", exact=True).click()
    conclusion = page.get_by_test_id("investigation-conclusion")
    expect(conclusion).to_contain_text("本次目标：直接故障定位")
    expect(conclusion).to_contain_text("catalog-worker 停止")
    expect(conclusion).to_contain_text("evidence-state")
    if entry_verified:
        expect(conclusion).to_contain_text("已验证入口影响")
        expect(conclusion).to_contain_text("https://isolated.invalid/api")
    else:
        expect(conclusion).to_contain_text("实例范围")
        expect(conclusion).to_contain_text("入口与业务影响尚未确认")


def test_hanging_json_body_expires_and_recovery_does_not_execute_actions(ui):
    page, origin = ui
    state = contract(page, saved=True)
    page.add_init_script("""const nativeFetch = window.fetch;
        window.fetch = (url, options) => {
            if (String(url).endsWith('/runs/run-original') && !sessionStorage.getItem('body_recovered'))
                return Promise.resolve(new Response(new ReadableStream({start() {}})));
            return nativeFetch(url, options);
        };""")
    page.clock.install()
    open_investigation(page, origin)
    expect(page.get_by_label("目标系统")).to_contain_text("Isolated business")
    page.clock.fast_forward(31_000)
    expect(page.get_by_test_id("run-connection")).to_contain_text("请求超时")
    expect(page.get_by_role("button", name="提交故障并开始调查", exact=False)).to_be_enabled()
    page.evaluate("sessionStorage.setItem('body_recovered', 'yes')")
    page.get_by_role("button", name="重新读取同一调查", exact=True).click()
    expect(page.locator(".run-view")).to_contain_text("执行状态：已结束")
    assert not state["posts"]


def test_disconnect_preserves_last_evidence_and_marks_execution_unknown(ui):
    page, origin = ui
    state = contract(page, saved=True)
    state["run"]["execution_status"] = "running"
    page.clock.install()
    open_investigation(page, origin)
    expect(page.locator(".run-view")).to_contain_text("执行状态：调查中")
    state["failure"] = "network"
    page.clock.fast_forward(2_000)
    expect(page.get_by_test_id("run-connection")).to_contain_text("调查状态暂时未知")
    expect(page.locator(".run-view")).to_contain_text("执行状态：连接状态未知")
    expect(page.get_by_test_id("investigation-conclusion")).to_contain_text("orders-api")
    expect(page.get_by_role("button", name="提交故障并开始调查", exact=False)).to_be_enabled()
    assert not state["posts"]


def test_legacy_supported_run_keeps_original_repair_path(ui):
    page, origin = ui
    state = contract(page)
    for field in ["diagnosis_status", "diagnosis_result", "completion_satisfied", "execution_status"]:
        state["run"].pop(field)
    state["run"]["summary"] = {"summary": "Historical verified diagnosis"}
    open_investigation(page, origin)
    page.locator(".incident-list").get_by_text("API 未响应", exact=True).click()
    expect(page.locator(".repair-form")).to_be_visible()
    expect(page.get_by_test_id("investigation-conclusion")).not_to_contain_text("直接故障定位目标已完成")
    expect(page.get_by_test_id("investigation-conclusion")).not_to_contain_text("尚不足以确认完整根因")


def test_restored_legacy_job_stops_polling_after_completion(ui):
    page, origin = ui
    state = contract(page, saved=True)
    for field in ["diagnosis_status", "diagnosis_result", "completion_satisfied", "execution_status"]:
        state["run"].pop(field)
    page.clock.install()
    open_investigation(page, origin)
    expect(page.locator(".run-view")).to_be_visible()
    expect(page.get_by_role("button", name="提交故障并开始调查", exact=False)).to_be_enabled()
    page.clock.fast_forward(2_000)
    assert "/jobs/" not in state["gets"]
    expect(page.get_by_test_id("run-connection")).to_have_count(0)


def test_efficiency_keeps_direct_findings_separate_from_complete_root_causes(ui):
    page, origin = ui
    state = contract(page)
    page.route("**/api/v1/metrics", lambda route: route.fulfill(json={
        "runs_total": 3, "runs_completed": 3, "supported_runs": 1,
        "direct_cause_runs": 2, "unresolved_runs": 0,
    }))
    open_investigation(page, origin)
    page.get_by_role("button", name="效果与成本", exact=False).click()
    expect(page.locator(".metric-card").filter(has_text="完整根因确认")).to_contain_text("1")
    expect(page.get_by_text("直接原因定位 2 次", exact=True)).to_be_visible()
    assert not state["posts"]


def test_waiting_for_scope_keeps_required_action_visible_without_expanding_audit(ui):
    page, origin = ui
    state = contract(page, diagnosis="partial")
    state["run"]["summary"] = {"stage": "awaiting_review", "discovery_ids": ["discovery-1"]}
    state["run"].pop("execution_status")
    open_investigation(page, origin)
    page.locator(".incident-list").get_by_text("API 未响应", exact=True).click()
    expect(page.get_by_role("button", name="审核运行范围", exact=True)).to_be_visible()
    expect(page.get_by_test_id("evidence-audit")).not_to_have_attribute("open", "")
