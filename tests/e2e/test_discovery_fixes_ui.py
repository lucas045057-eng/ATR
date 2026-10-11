"""Real browser/API, isolated data; the model endpoint is local fake HTTP only."""
import json
import os

import httpx
from playwright.sync_api import expect, sync_playwright
from test_autonomous_discovery import mapping, unbound
from test_browser import live_server  # noqa: F401
from test_business import onboard
from test_media_optional import PNG
from test_model_isolation import fake_model  # noqa: F401
from test_v22_adaptive import report


def browser_page(p, base, token):
    browser = p.chromium.launch(headless=True, channel=os.environ.get("ATR_BROWSER_CHANNEL") or None)
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ")")
    page = context.new_page()
    page.goto(base)
    return browser, page


def test_browser_discovery_review_and_same_run_resume(live_server, fake_model):  # noqa: F811
    base, directory = live_server
    _, _, state, _ = fake_model
    token = "browser-test-operator-" + "x" * 40
    state["reply"] = lambda c, _: mapping(c) if c.get("phase") == "discovery" else report()
    with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token}) as api:
        profile, _ = unbound(api)
        api.post("/api/v1/incidents", json={"system_id": profile["system_id"], "symptom": "catalog-api discovery UI"}).raise_for_status()
    with sync_playwright() as p:
        browser, page = browser_page(p, base, token)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept("人工核验隔离服务身份及授权范围"))
        page.get_by_role("button", name="故障调查", exact=False).first.click()
        page.locator(".incident-row").first.click()
        page.get_by_role("button", name="开始只读调查", exact=False).click()
        expect(page.get_by_role("heading", name="自主发现与范围建议")).to_be_visible(timeout=30000)
        expect(page.get_by_text("Agent 阶段：", exact=False)).to_contain_text("等待范围审核")
        expect(page.get_by_text("反证或冲突：", exact=False)).to_be_visible()
        page.screenshot(path=str(directory / "discovery-awaiting-review.png"), full_page=True)
        page.get_by_role("button", name="审核运行范围", exact=True).click()
        expect(page.get_by_role("heading", name="发现的运行对象")).to_be_visible()
        process = page.locator("details").filter(has=page.locator("summary").filter(has_text="进程线索")).last
        assert process.get_attribute("open") is None
        row = page.locator(".business-row").filter(has_text="catalog-api → catalog-api.service")
        row.get_by_role("button", name="确认", exact=True).click()
        expect(row.get_by_role("button", name="确认", exact=True)).to_have_count(0, timeout=10000)
        page.get_by_role("button", name="故障调查", exact=False).first.click()
        page.get_by_role("button", name="审核后继续调查", exact=True).click()
        expect(page.get_by_text("Agent 阶段：", exact=False)).to_contain_text("已完成", timeout=30000)
        expect(page.get_by_text("尚未建立根因", exact=True)).to_be_visible()
        assert state["requests"] == 2 and not errors
        page.screenshot(path=str(directory / "discovery-resumed-unresolved.png"), full_page=True)
        browser.close()


def test_browser_optional_attachment_undo_and_process_groups(live_server):  # noqa: F811
    base, directory = live_server
    token = os.environ.get("ATR_E2E_OPERATOR_TOKEN") if os.environ.get("ATR_E2E_BASE_URL") else "browser-test-operator-" + "x" * 40
    with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token}) as api:
        profile = onboard(api, {"favicon.png": PNG, "architecture.png": PNG})
    with sync_playwright() as p:
        browser, page = browser_page(p, base, token)
        page.get_by_role("button", name="业务档案", exact=False).first.click()
        page.get_by_label("选择业务").select_option(profile["business_id"])
        page.locator("summary").filter(has_text="查看文件清单").click()
        expect(page.get_by_text("可选静态附件 1 个", exact=False)).to_be_visible()
        expect(page.locator(".business-row").filter(has_text="architecture.png")).to_contain_text("待人工录入")
        page.get_by_role("button", name="撤销静态附件分类", exact=False).click()
        expect(page.get_by_text("可选静态附件 0 个", exact=False)).to_have_count(1, timeout=10000)
        page.locator("summary").filter(has_text="查看文件清单").click()
        page.get_by_role("button", name="勾选明确图标", exact=False).click()
        page.get_by_role("button", name="批量设为可选附件", exact=True).click()
        expect(page.get_by_text("可选静态附件 1 个", exact=False)).to_have_count(1, timeout=10000)
        page.locator("summary").filter(has_text="查看文件清单").click()
        expect(page.get_by_text("可选静态附件 1 个", exact=False)).to_be_visible(timeout=10000)
        page.screenshot(path=str(directory / "optional-media-review.png"), full_page=True)
        browser.close()
