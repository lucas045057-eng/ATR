import json
import os

import httpx
import paramiko
import pytest
from playwright.sync_api import expect, sync_playwright
from test_browser import live_server  # noqa: F401


@pytest.fixture
def ssh_server_enabled():
    return True


def test_execution_and_auth_choices_and_docker_default(live_server, ssh_server_enabled):  # noqa: F811
    base, directory = live_server
    token = "browser-test-operator-" + "x" * 40
    with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token}) as api:
        api.post("/api/v1/businesses", json={"name": "普通 SSH 页面验收"}).raise_for_status()
    key = paramiko.RSAKey.generate(1024)
    password = "synthetic-standard-browser-memory-only"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=os.environ.get("ATR_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ")")
        page = context.new_page()
        submitted = []
        page.on("request", lambda r: submitted.append((r.url, r.post_data or "")))
        page.goto(base)
        page.get_by_role("button", name="服务器范围", exact=False).click()
        page.get_by_label("连接方式").select_option("ssh")
        expect(page.get_by_label("已授权受限的 Docker", exact=False)).to_be_checked()
        for mode, auth in (("standard_ssh_readonly", "private_key"), ("standard_ssh_readonly", "password"),
                           ("forced_gateway", "private_key"), ("forced_gateway", "password")):
            page.get_by_label("SSH 执行方式").select_option(mode)
            page.get_by_label("认证方式").select_option(auth)
            page.get_by_label("环境名称", exact=True).fill(mode + "-" + auth)
            page.get_by_label("地址", exact=True).fill("127.0.0.1")
            page.get_by_label("受限用户名").fill("atrstandard")
            page.get_by_label("已核对的主机公钥").fill(f"{key.get_name()} {key.get_base64()}")
            if mode == "standard_ssh_readonly":
                expect(page.get_by_text("允许 AI 调用已授权只读工具", exact=False).first).to_be_visible()
            page.get_by_label("权限边界确认").fill("已核对合成账号和授权范围，并理解客户端权限边界")
            page.get_by_label("已授权访问这台测试服务器").check()
            if auth == "password":
                expect(page.get_by_label("凭据引用", exact=True)).to_have_count(0)
                page.get_by_label("SSH 密码", exact=True).fill(password)
            else:
                page.get_by_label("凭据引用", exact=True).fill("synthetic-key-ref")
            page.get_by_role("button", name="保存环境关联", exact=True).click()
            expect(page.get_by_role("button", name="保存环境关联", exact=True)).to_be_enabled(timeout=10000)
            expect(page.get_by_label("运行环境").locator("option:checked")).to_contain_text(mode + "-" + auth)
        stored = page.evaluate("JSON.stringify({local:{...localStorage},session:{...sessionStorage}})")
        assert password not in stored
        secret_requests = [(url, body) for url, body in submitted if password in body]
        assert len(secret_requests) == 2 and all(url.endswith("/credential") for url, _ in secret_requests)
        target_requests = [json.loads(body) for url, body in submitted if url.endswith("/targets") and body]
        assert len(target_requests) == 4 and all(t["docker_reads_enabled"] is True for t in target_requests)
        page.get_by_label("SSH 执行方式").select_option("standard_ssh_readonly")
        expect(page.get_by_label("权限边界确认")).to_have_value("")
        expect(page.get_by_label("已授权访问这台测试服务器")).not_to_be_checked()
        page.evaluate("window.scrollTo(0,0)")
        page.screenshot(path=str(directory / "standard-ssh-modes.png"), full_page=True)
        browser.close()
