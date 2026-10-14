"""Actual browser + actual local API + explicit Mock runtime. No mocked HTTP responses."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright
from test_business import SAMPLE, archive

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def live_server(tmp_path, request):
    external = os.environ.get("ATR_E2E_BASE_URL")
    if external:
        assert "fake_model" not in request.fixturenames, "模型错误测试必须启动专用隔离 API"
        parsed = urlsplit(external)
        assert parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}, "仅允许隔离回环验收"
        assert os.environ.get("ATR_E2E_OPERATOR_TOKEN"), "隔离验收需要专用本地 token"
        yield external.rstrip("/"), tmp_path
        return
    if not (ROOT / "frontend/dist/index.html").exists():
        pytest.skip("先运行前端 npm run build，再执行浏览器验收")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = dict(
        os.environ,
        DATABASE_PATH=str(tmp_path / "runtime.sqlite"),
        ARTIFACT_ROOT=str(tmp_path / "artifacts"),
        REPOSITORY_ROOTS=str(ROOT / "demo"),
        OPERATOR_TOKEN="browser-test-operator-" + "x" * 40,
        COLLECTOR_TOKEN="browser-test-collector-" + "y" * 40,
        EXTERNAL_MODEL_ENABLED="false",
        SSH_ENABLED="true" if "ssh_server_enabled" in request.fixturenames else "false",
        MODEL_NAME="",
        MODEL_API_KEY="",
        PYTHONPATH=str(ROOT / "backend"),
    )
    if "fake_model" in request.fixturenames:
        _, model_settings, _, _ = request.getfixturevalue("fake_model")
        env.update(EXTERNAL_MODEL_ENABLED="true", MODEL_NAME=model_settings.model_name,
                   MODEL_API_KEY=model_settings.model_api_key.get_secret_value(),
                   MODEL_BASE_URL=model_settings.model_base_url, MODEL_TIMEOUT_SECONDS="2")
    log = (tmp_path / "server.log").open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/api/v1/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("本地 API 未启动")
        yield base, tmp_path
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()


def test_browser_complete_onboarding(live_server):
    base, directory = live_server
    token = os.environ.get("ATR_E2E_OPERATOR_TOKEN") if os.environ.get("ATR_E2E_BASE_URL") else "browser-test-operator-" + "x" * 40
    existing = httpx.post(base + "/api/v1/systems/onboard", headers={"Authorization": "Bearer " + token},
                          json={"source_key": "system-a", "display_name": "Existing isolated system"})
    assert existing.status_code == 200, existing.text
    source = directory / "catalog.zip"
    source.write_bytes(archive(SAMPLE))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel=os.environ.get("ATR_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_init_script("sessionStorage.setItem('atr_operator_token'," + json.dumps(token) + ")")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept("人工核验隔离测试资料与运行对象，允许只读探索"))
        page.goto(base)
        page.get_by_role("button", name="导入业务资料", exact=False).click()
        page.get_by_label("业务名称", exact=True).fill("浏览器 Catalog")
        page.get_by_label("功能描述", exact=True).fill("隔离的业务接入验收")
        page.get_by_role("button", name="创建并保存", exact=True).click()
        expect(page.get_by_role("button", name="上传并建立索引")).to_be_enabled(timeout=15000)
        page.get_by_label("业务资料文件").set_input_files(source)
        page.get_by_role("button", name="上传并建立索引").click()
        expect(page.get_by_text("已处理 5 文件", exact=False)).to_be_visible(timeout=20000)
        page.get_by_role("button", name="审核并保存档案").click()
        expect(page.get_by_role("button", name="审核并保存档案")).to_be_enabled(timeout=10000)
        page.get_by_role("button", name="业务概览", exact=False).click()
        expect(page.get_by_role("heading", name="浏览器 Catalog", exact=True)).to_be_visible()
        expect(page.get_by_role("img", name="业务服务和模块依赖图")).to_be_visible()
        page.get_by_label("源码检索关键词").fill("record")
        page.get_by_role("button", name="检索", exact=True).click()
        expect(page.get_by_role("button", name="读取局部源码").first).to_be_visible()
        page.get_by_role("button", name="读取局部源码").first.click()
        expect(page.locator(".code-preview")).to_contain_text("def record")
        page.evaluate("window.scrollTo(0,0)")
        page.screenshot(path=str(directory / "business-profile.png"), full_page=False)
        page.get_by_role("button", name="服务器与服务", exact=False).click()
        page.get_by_label("授权业务目录（每行一个）").fill("/srv/catalog")
        page.get_by_role("button", name="保存环境关联").click()
        expect(page.get_by_role("button", name="定向轻量发现")).to_be_enabled()
        page.get_by_role("button", name="定向轻量发现").click()
        expect(page.get_by_role("button", name="确认", exact=True).first).to_be_visible(timeout=15000)
        page.get_by_role("button", name="确认", exact=True).first.click()
        expect(page.get_by_text("已确认", exact=True)).to_be_visible()
        page.get_by_role("button", name="排查故障", exact=False).click()
        expect(page.get_by_label("目标系统")).to_contain_text("浏览器 Catalog")
        expect(page.get_by_label("目标系统").locator("option:checked")).to_have_text("浏览器 Catalog")
        target_selector = page.get_by_label("本次调查 SSH 目标")
        expect(target_selector).to_be_visible()
        expect(target_selector.locator("option:checked")).to_contain_text("隔离测试环境")
        page.get_by_label("故障现象", exact=True).fill("catalog-api startup failure")
        page.get_by_role("button", name="保存现象，准备调查", exact=False).click()
        expect(page.get_by_role("button", name="开始只读调查", exact=False)).to_be_visible()
        page.get_by_role("button", name="开始只读调查", exact=False).click()
        expect(page.get_by_text("尚未建立根因", exact=True)).to_be_visible(timeout=20000)
        expect(page.locator(".target-binding")).to_contain_text("隔离测试环境")
        expect(page.locator(".target-binding")).to_contain_text("forced_gateway")
        expect(page.get_by_text("未解决原因：", exact=False)).to_be_visible()
        page.evaluate("window.scrollTo(0,0)")
        page.screenshot(path=str(directory / "investigation.png"), full_page=False)
        page.get_by_role("button", name="排障经验", exact=False).click()
        expect(page.get_by_role("heading", name="待复核提醒与失败反例")).to_be_visible()
        expect(page.get_by_text("项未验证反例", exact=False).first).to_be_visible()
        page.get_by_role("button", name="效果与成本", exact=False).click()
        expect(page.get_by_role("heading", name="三模式完整对比")).to_be_visible()
        expect(page.get_by_role("cell", name="按当前证据逐步排查", exact=True)).to_be_visible()
        assert errors == []
        browser.close()
