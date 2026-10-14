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


def test_password_form_has_no_browser_persistence(live_server, ssh_server_enabled):  # noqa: F811
    base, directory = live_server
    token = "browser-test-operator-" + "x" * 40
    with httpx.Client(base_url=base, headers={"Authorization": "Bearer " + token}) as api:
        business = api.post("/api/v1/businesses", json={"name": "SSH 密码浏览器验收", "description": "隔离合成数据"})
        business.raise_for_status()
    password = "synthetic-browser-ssh-6f3e!"
    key = paramiko.RSAKey.generate(1024)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=os.environ.get("ATR_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ")")
        page = context.new_page()
        submitted = []
        page.on("request", lambda r: submitted.append((r.url, r.post_data or "")))
        page.goto(base)
        page.get_by_role("button", name="服务器与服务", exact=False).click()
        page.get_by_label("连接方式").select_option("ssh")
        page.get_by_label("认证方式").select_option("password")
        expect(page.get_by_label("凭据引用", exact=True)).to_have_count(0)
        page.get_by_label("地址", exact=True).fill("127.0.0.1")
        page.get_by_label("受限用户名").fill("atrreader")
        page.get_by_label("已核对的主机公钥").fill(f"{key.get_name()} {key.get_base64()}")
        page.get_by_label("权限边界确认").fill("已核对隔离服务器的 ForceCommand 与禁用转发")
        page.get_by_label("已授权访问这台测试服务器").check()
        page.get_by_label("SSH 密码", exact=True).fill(password)
        page.get_by_role("button", name="保存环境关联", exact=True).click()
        expect(page.get_by_text("密码会话有效至", exact=False)).to_be_visible()
        expect(page.get_by_label("SSH 密码", exact=True)).to_have_value("")
        assert password not in page.evaluate("JSON.stringify({local: {...localStorage},session: {...sessionStorage}})")
        secret_posts = [(url, body) for url, body in submitted if password in body]
        assert len(secret_posts) == 1 and secret_posts[0][0].endswith("/credential")
        assert all(password not in url for url, _ in submitted)
        page.get_by_role("button", name="清除密码会话", exact=True).click()
        expect(page.get_by_role("button", name="定向轻量发现", exact=True)).to_be_disabled()
        expect(page.get_by_text("密码会话未提供、已过期或已清除", exact=False)).to_be_visible()
        page.get_by_label("重新提供 SSH 密码").fill(password)
        page.get_by_role("button", name="更新密码会话", exact=True).click()
        expect(page.get_by_role("button", name="定向轻量发现", exact=True)).to_be_enabled()
        expect(page.get_by_label("重新提供 SSH 密码")).to_have_value("")
        page.screenshot(path=str(directory / "ssh-password.png"))
        page.reload()
        page.get_by_role("button", name="服务器与服务", exact=False).click()
        expect(page.get_by_text("密码会话有效至", exact=False)).to_be_visible()
        expect(page.get_by_label("重新提供 SSH 密码")).to_have_value("")
        assert password not in page.evaluate("JSON.stringify({local: {...localStorage},session: {...sessionStorage}})")
        browser.close()
