from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "docs" / "fixtures" / "catalog-onboarding.zip"
TERMINAL_JOB_STATES = {"completed", "failed", "cancelled", "timed_out"}
TERMINAL_RUN_STATES = {"completed", "failed", "timed_out", "interrupted", "cancelled"}


class DemoFlowError(RuntimeError):
    pass


class StopBeforeRun(RuntimeError):
    pass


class ManualPlaywright:
    """Keep Playwright alive long enough to close the context and save the video."""

    def __enter__(self):
        self._playwright = sync_playwright().start()
        return self._playwright

    def __exit__(self, exc_type, exc, traceback_object) -> bool:
        return False

    def stop(self) -> None:
        playwright = getattr(self, "_playwright", None)
        if playwright is not None:
            playwright.stop()
            self._playwright = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def api_base(base_url: str) -> str:
    return base_url.rstrip("/") + "/api/v1"


def api_path(url: str) -> str:
    return urlsplit(url).path


def list_value(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        value = payload.get("value", [])
        return value if isinstance(value, list) else []
    return payload if isinstance(payload, list) else []


def get_json(client: httpx.Client, path: str) -> Any:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def wait_for_job(client: httpx.Client, job_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = get_json(client, f"/jobs/{job_id}")
        if last.get("status") in TERMINAL_JOB_STATES:
            if last.get("status") != "completed":
                raise DemoFlowError(f"后台任务 {job_id} 未成功完成：{last.get('status')} {last.get('error', '')}")
            return last
        time.sleep(0.35)
    raise DemoFlowError(f"等待后台任务 {job_id} 超时，最后状态：{last}")


def wait_for_artifact_completion(client: httpx.Client, business_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        artifacts = list_value(get_json(client, f"/businesses/{business_id}/artifacts"))
        if artifacts:
            last = artifacts[0]
            status = last.get("status")
            if status == "completed":
                return {
                    "status": status,
                    "result": {"artifact_id": last.get("id")},
                    "artifact_id": last.get("id"),
                }
            if status in {"failed", "cancelled"}:
                raise DemoFlowError(
                    f"资料处理失败：artifact={last.get('id')} status={status} "
                    f"error={last.get('error')}"
                )
        time.sleep(0.35)
    raise DemoFlowError(f"等待资料处理超时，最后 artifact 状态：{last}")


def current_session_job_id(page: Page) -> str:
    value = page.evaluate(
        """() => {
            try {
                const raw = sessionStorage.getItem('atr_active_business_job');
                return raw ? (JSON.parse(raw).job_id || null) : null;
            } catch {
                return null;
            }
        }"""
    )
    return str(value) if value else ""


def wait_for_incident_run(client: httpx.Client, system_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        last = list_value(get_json(client, "/incidents"))
        match = next(
            (
                item
                for item in last
                if item.get("system_id") == system_id and item.get("latest_run_id")
            ),
            None,
        )
        if match:
            return match
        time.sleep(0.5)
    raise DemoFlowError(f"等待事件与调查启动超时；当前事件：{last[:3]}")


def wait_for_run(client: httpx.Client, run_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = get_json(client, f"/runs/{run_id}")
        execution = last.get("execution_status")
        if execution == "waiting_input":
            raise DemoFlowError(
                f"调查 {run_id} 等待额外范围确认；脚本不会自动扩大授权。请检查业务授权与默认范围。"
            )
        if execution in TERMINAL_RUN_STATES:
            return last
        if not execution and (last.get("ended_at") or last.get("status") in {"supported", "unresolved", "completed"}):
            return last
        time.sleep(0.8)
    raise DemoFlowError(f"等待调查 {run_id} 超时，最后状态：{last.get('execution_status') or last.get('status')}")


def scroll_click(page: Page, locator, *, pause_ms: int = 600) -> None:
    locator.scroll_into_view_if_needed()
    page.wait_for_timeout(250)
    locator.click()
    page.wait_for_timeout(pause_ms)


def shot(page: Page, directory: Path, name: str) -> str:
    path = directory / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def response_is(response, method: str, path: str) -> bool:
    return response.request.method == method and api_path(response.url) == path


def response_suffix_is(response, method: str, suffix: str) -> bool:
    return response.request.method == method and api_path(response.url).endswith(suffix)


@contextmanager
def capture_responses(page: Page, predicate):
    captured: list[dict[str, Any]] = []

    def handle_response(response) -> None:
        if not predicate(response):
            return
        item: dict[str, Any] = {
            "status": response.status,
            "method": response.request.method,
            "path": api_path(response.url),
            "_response": response,
        }
        try:
            item["json"] = response.json()
        except Exception:
            try:
                item["text"] = response.text()
            except Exception:
                item["text"] = "<response body unavailable>"
        captured.append(item)

    page.on("response", handle_response)
    try:
        yield captured
    finally:
        page.remove_listener("response", handle_response)


def captured_json(item: dict[str, Any], *, description: str) -> Any:
    if "json" in item:
        return item["json"]
    response = item.get("_response")
    if response is not None:
        try:
            payload = response.json()
        except Exception:
            pass
        else:
            item["json"] = payload
            return payload
    raise DemoFlowError(
        f"{description}响应不是可解析 JSON：HTTP {item.get('status')} "
        f"{item.get('text', '<body unavailable>')}"
    )


def wait_capture(
    page: Page,
    captures: list[dict[str, Any]],
    predicate,
    *,
    description: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        match = next((item for item in captures if predicate(item)), None)
        if match:
            return match
        page.wait_for_timeout(120)
    raise DemoFlowError(f"等待 {description} 响应超时")


def select_if_needed(page: Page, locator, value: str, pause_ms: int = 600) -> bool:
    current = locator.input_value()
    if current == value:
        page.wait_for_timeout(pause_ms)
        return False
    locator.select_option(value)
    page.wait_for_timeout(pause_ms)
    return True


def run_usage(run: dict) -> dict:
    budget = run.get("context_budget") or {}
    return {
        "model_calls": budget.get("calls", 0),
        "input_tokens": budget.get("input_tokens", 0),
        "output_tokens": budget.get("output_tokens", 0),
        "tool_calls": budget.get("tool_calls", 0),
        "retrieved_bytes": budget.get("retrieved_bytes", 0),
    }


def compact_run(run: dict) -> dict:
    summary = run.get("summary") or {}
    report = summary.get("report", {}).get("fact_report", {})
    result = run.get("diagnosis_result") or summary.get("diagnosis_result") or {}
    return {
        "id": run.get("id"),
        "incident_id": run.get("incident_id"),
        "execution_status": run.get("execution_status"),
        "diagnosis_status": run.get("diagnosis_status") or result.get("diagnosis_status"),
        "trigger_cause_status": result.get("trigger_cause_status"),
        "completion_satisfied": run.get("completion_satisfied"),
        "summary": report.get("summary") or summary.get("summary"),
        "decision_protocol": summary.get("decision_protocol"),
        "stop_reason": summary.get("stop_reason"),
        "usage": run_usage(run),
        "tool_calls": [
            {
                "tool_id": item.get("tool_id"),
                "status": item.get("status"),
                "args": item.get("args_redacted_json"),
                "call_id": item.get("id"),
            }
            for item in run.get("tool_calls", [])
        ],
        "fact_report": {
            "summary": report.get("summary"),
            "direct_anomalies": report.get("direct_anomalies", []),
            "confirmed_facts": report.get("confirmed_facts", []),
            "unknown_causes": report.get("unknown_causes", []),
            "missing_evidence": report.get("missing_evidence", []),
            "checks": report.get("checks", []),
        },
        "required_gaps": result.get("required_gaps", []),
    }


def write_report(
    path: Path,
    *,
    base_url: str,
    started_at: str,
    finished_at: str,
    fixture: Path,
    business_name: str,
    steps: list[dict],
    prior_runs: list[dict],
    run: dict | None,
    error: str | None,
    video_path: str | None,
    screenshots: list[str],
) -> None:
    lines = [
        "# ATR Hackathon Demo Verification",
        "",
        f"- 执行时间：`{started_at}` -> `{finished_at}`",
        f"- 运行地址：`{base_url}`",
        f"- 演示业务：`{business_name}`",
        f"- 输入资料：`{fixture}`",
        "- 驱动方式：Playwright 驱动真实 Web UI；API 仅用于只读轮询和保存证据。",
        f"- 视频：`{video_path or '未生成'}`",
        f"- 总状态：{'已完成' if error is None and run else '未完成'}",
        "",
        "## 执行步骤",
        "",
    ]
    for index, step in enumerate(steps, 1):
        lines.append(f"### {index}. {step['name']}")
        lines.append("")
        lines.append(f"- 状态：`{step['status']}`")
        if step.get("details"):
            lines.append(f"- 结果：{step['details']}")
        lines.append("")

    lines.extend(["## 调查结果", ""])
    if run:
        result = compact_run(run)
        lines.extend(
            [
                f"- Run ID：`{result['id']}`",
                f"- 执行状态：`{result['execution_status']}`",
                f"- 结论等级：`{result['diagnosis_status']}`",
                f"- 触发原因状态：`{result['trigger_cause_status']}`",
                f"- 完成标记：`{result['completion_satisfied']}`",
                f"- 决策协议：`{result['decision_protocol']}`",
                f"- 事实摘要：{result['summary'] or '未提供'}",
                "",
                "### Run 结果 JSON 片段",
                "",
                "```json",
                json.dumps(result, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
        if result["diagnosis_status"] != "root_cause_supported":
            lines.extend(
                [
                    "> 证据门没有把本次调查升级为完整根因。报告保留直接异常、日志线索和证据缺口，",
                    "> 不把 Mock 日志中的 “datastore unavailable” 改写成已独立验证的因果链。",
                    "",
                ]
            )
    else:
        lines.extend(["- 本次没有形成可引用的最终 Run。", ""])

    lines.extend(["## 模型用量", ""])
    prior_usage = [{"run_id": item.get("id"), **run_usage(item)} for item in prior_runs]
    current_usage = run_usage(run) if run else {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_calls": 0,
        "retrieved_bytes": 0,
    }
    lines.extend(
        [
            f"- 演示前已有完整 Run：`{len(prior_runs)}` 次。",
            f"- 本次新增完整 Run：`{1 if run else 0}` 次。",
            f"- 本次 Run 模型调用：`{current_usage['model_calls']}` 次；"
            f"输入 `{current_usage['input_tokens']}` / 输出 `{current_usage['output_tokens']}` tokens；"
            f"工具调用 `{current_usage['tool_calls']}` 次。",
            "- 与任务上限核对：演示前 Run + 本次 Run 不超过 2 次。",
            "",
            "```json",
            json.dumps({"prior_runs": prior_usage, "demo_run": current_usage}, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )

    if screenshots:
        lines.extend(["## 页面截图", ""])
        for item in screenshots:
            lines.append(f"- `{item}`")
        lines.append("")

    if error:
        lines.extend(["## 未解决或失败", "", "```text", error, "```", ""])
    else:
        lines.extend(
            [
                "## 限制与注意事项",
                "",
                "- 本演示使用 Mock 隔离目标；Mock 结果不能作为真实 Linux 验收。",
                "- 现场未执行故障注入、重启、修复或任何写操作。",
                "- 视频展示的是本次真实 UI 操作和真实后端结果，没有后期篡改结论等级。",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Record the ATR hackathon catalog demo.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output")
    parser.add_argument("--business-name", default="")
    parser.add_argument("--browser-channel", default="chrome")
    parser.add_argument("--timeout", type=float, default=360.0)
    parser.add_argument(
        "--stop-before-run",
        action="store_true",
        help="Exercise and record setup through scope confirmation without starting a Run.",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    api = api_base(base_url)
    fixture = args.fixture.resolve()
    output_dir = args.output_dir.resolve()
    screenshot_dir = output_dir / "screenshots"
    video_dir = output_dir / "videos"
    report_path = output_dir / "demo-verification.md"
    final_video = output_dir / "atr-hackathon-demo.webm"
    for directory in (output_dir, screenshot_dir, video_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if not fixture.is_file():
        raise SystemExit(f"Fixture not found: {fixture}")

    started_at = now_iso()
    business_name = args.business_name.strip() or (
        "ATR Hackathon Catalog " + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    steps: list[dict] = []
    screenshots: list[str] = []
    prior_runs: list[dict] = []
    business_id = ""
    system_id = ""
    target_id = ""
    incident_id = ""
    run_id = ""
    final_run: dict | None = None
    flow_error: str | None = None
    browser = None
    context = None
    video = None
    playwright_session: ManualPlaywright | None = None

    with httpx.Client(base_url=api, timeout=30.0) as client:
        health = get_json(client, "/health")
        if health.get("status") != "ready":
            raise SystemExit(f"ATR API is not ready: {health}")
        prior_summaries = list_value(get_json(client, "/runs"))
        prior_runs = [get_json(client, f"/runs/{item['id']}") for item in prior_summaries]
        if len(prior_runs) >= 2:
            raise SystemExit(
                "Refusing to create another full investigation: "
                f"{len(prior_runs)} runs already exist (task cap is 2)."
            )
        print(f"API ready; prior full runs: {len(prior_runs)}")

        try:
            playwright_session = ManualPlaywright()
            with playwright_session as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    channel=args.browser_channel or None,
                )
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    device_scale_factor=1,
                    record_video_dir=str(video_dir),
                    record_video_size={"width": 1280, "height": 800},
                )
                page = context.new_page()
                video = page.video
                page.set_default_timeout(20_000)
                page.set_default_navigation_timeout(45_000)
                page_errors: list[str] = []
                http_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on(
                    "response",
                    lambda response: http_errors.append(
                        f"{response.request.method} {api_path(response.url)} -> {response.status}"
                    )
                    if "/api/v1/" in response.url and response.status >= 400
                    else None,
                )

                intro = """
                <!doctype html>
                <html lang="zh-CN">
                <meta charset="utf-8">
                <style>
                  body { margin: 0; background: #0b1720; color: #f3efe4;
                    font-family: "Microsoft YaHei", "Noto Sans SC", sans-serif; }
                  main { min-height: 100vh; display: grid; place-content: center; padding: 72px; }
                  .kicker { color: #efb64b; letter-spacing: .24em; font: 700 14px monospace; }
                  h1 { font-size: 62px; line-height: 1.05; margin: 20px 0 18px; max-width: 900px; }
                  p { color: #aec2c8; font-size: 22px; line-height: 1.6; max-width: 820px; }
                  .rule { width: 120px; height: 5px; background: #efb64b; margin-top: 34px; }
                </style>
                <main>
                  <div class="kicker">SPRINT DEMO / EVIDENCE GATE</div>
                  <h1>ATR 自适应只读排障</h1>
                  <p>从陌生业务接入、人工范围确认，到只读调查与证据门裁决。</p>
                  <p>本次演示使用隔离 Mock 目标，不改写调查结论。</p>
                  <div class="rule"></div>
                </main>
                </html>
                """
                page.set_content(intro)
                page.wait_for_timeout(2_200)
                page.goto(base_url, wait_until="domcontentloaded")
                page.get_by_role("heading", name="业务概览", exact=True).wait_for()
                page.wait_for_timeout(900)

                page.get_by_role("button", name="导入业务资料", exact=False).first.click()
                page.locator("h1").filter(has_text="导入业务资料").wait_for()
                page.wait_for_timeout(700)

                create_panel = page.locator("section.panel").filter(
                    has=page.get_by_role("heading", name="创建业务概览", exact=True)
                ).first
                create_panel.locator('input[name="name"]').fill(business_name)
                create_panel.locator('textarea[name="description"]').fill(
                    "catalog-api depends on store; isolated hackathon read-only demo."
                )
                page.wait_for_timeout(700)
                with capture_responses(
                    page,
                    lambda response: response_is(
                        response, "POST", f"{api_path(api + '/businesses')}"
                    ),
                ) as create_captures:
                    scroll_click(
                        page,
                        create_panel.get_by_role("button", name="创建并保存"),
                        pause_ms=0,
                    )
                    created_response = wait_capture(
                        page,
                        create_captures,
                        lambda item: item["path"] == "/api/v1/businesses",
                        description="创建业务",
                    )
                if created_response["status"] not in {200, 201}:
                    raise DemoFlowError(
                        f"创建业务失败：HTTP {created_response['status']} "
                        f"{created_response.get('text', created_response.get('json'))}"
                    )
                created = captured_json(created_response, description="创建业务")
                business_id = created["business_id"]
                system_id = created["system_id"]
                expect(page.get_by_label("选择业务", exact=True)).to_have_value(business_id)
                page.wait_for_timeout(800)
                steps.append(
                    {
                        "name": "新业务接入",
                        "status": "completed",
                        "details": f"UI 创建业务 `{business_name}`；business_id=`{business_id}`，system_id=`{system_id}`。",
                    }
                )

                upload_panel = page.locator("section.panel").filter(
                    has=page.get_by_role("heading", name="导入业务资料", exact=True)
                ).first
                page.get_by_label("业务资料文件", exact=True).set_input_files(str(fixture))
                page.wait_for_timeout(700)
                with capture_responses(
                    page,
                    lambda response: response.request.method == "POST"
                    and api_path(response.url)
                    == f"/api/v1/businesses/{business_id}/artifacts",
                ) as upload_captures:
                    scroll_click(
                        page,
                        upload_panel.get_by_role("button", name="上传并建立索引", exact=True),
                        pause_ms=0,
                    )
                    upload_response = wait_capture(
                        page,
                        upload_captures,
                        lambda item: item["path"].endswith("/artifacts"),
                        description="上传资料",
                    )
                if upload_response["status"] != 202:
                    raise DemoFlowError(
                        f"上传资料失败：HTTP {upload_response['status']} "
                        f"{upload_response.get('text', upload_response.get('json'))}"
                    )
                upload_body_available = "json" in upload_response
                if upload_body_available:
                    upload = captured_json(upload_response, description="上传资料")
                    parse_job = wait_for_job(client, upload["job_id"], args.timeout)
                else:
                    upload = {
                        "job_id": current_session_job_id(page),
                        "artifact_id": None,
                        "status": "queued",
                    }
                    if upload["job_id"]:
                        parse_job = wait_for_job(client, upload["job_id"], args.timeout)
                    else:
                        parse_job = wait_for_artifact_completion(
                            client, business_id, args.timeout
                        )
                expect(page.get_by_text("后台任务：completed", exact=False)).to_be_visible()
                page.wait_for_timeout(900)
                profile = get_json(client, f"/businesses/{business_id}")
                if profile.get("coverage", {}).get("files") != 5:
                    raise DemoFlowError(f"静态解析文件数异常：{profile.get('coverage')}")
                steps.append(
                    {
                        "name": "后台静态解析",
                        "status": "completed",
                        "details": f"job_id=`{upload['job_id'] or '响应体不可读，按 artifact 轮询'}`，"
                        f"status=`{parse_job['status']}`，处理文件 "
                        f"`{parse_job.get('result', {}).get('files')}` 个；画像 v{profile['profile_version']}，"
                        f"静态解析 `{profile['coverage']['static_parsed']}/{profile['coverage']['files']}`。",
                    }
                )
                screenshots.append(shot(page, screenshot_dir, "01-onboarding-parsed"))

                page.get_by_role("button", name="业务概览", exact=False).first.click()
                page.get_by_role("heading", name="业务概览", exact=True).wait_for()
                expect(page.get_by_label("选择业务", exact=True)).to_have_value(business_id)
                page.wait_for_timeout(900)

                role_changes = []
                for service, desired in (("catalog-api", "application"), ("store", "datastore")):
                    role_select = page.get_by_label(f"{service}角色", exact=True)
                    role_select.scroll_into_view_if_needed()
                    if select_if_needed(page, role_select, desired):
                        role_changes.append(f"{service}->{desired}")
                page.wait_for_timeout(700)

                dependency_row = (
                    page.locator(".business-row")
                    .filter(has_text="service:catalog-api")
                    .filter(has_text="depends_on")
                    .filter(has_text="service:store")
                    .first
                )
                dependency_row.scroll_into_view_if_needed()
                page.wait_for_timeout(700)
                if "已确认" not in dependency_row.inner_text():
                    with capture_responses(
                        page,
                        lambda response: response_is(
                            response, "POST", f"/api/v1/businesses/{business_id}/review"
                        ),
                    ) as review_relation_captures:
                        scroll_click(
                            page,
                            dependency_row.get_by_role("button", name="确认", exact=True),
                            pause_ms=0,
                        )
                        review_relation = wait_capture(
                            page,
                            review_relation_captures,
                            lambda item: item["path"].endswith("/review"),
                            description="确认依赖关系",
                        )
                    if review_relation["status"] != 200:
                        raise DemoFlowError(
                            f"确认依赖关系失败：HTTP {review_relation['status']} "
                            f"{review_relation.get('text', review_relation.get('json'))}"
                        )
                expect(
                    page.locator(".business-row")
                    .filter(has_text="service:catalog-api")
                    .filter(has_text="depends_on")
                    .filter(has_text="已确认")
                    .first
                ).to_be_visible()

                confirm_profile = page.get_by_role("button", name="确认档案", exact=True).first
                with capture_responses(
                    page,
                    lambda response: response_is(
                        response, "POST", f"/api/v1/businesses/{business_id}/review"
                    ),
                ) as review_profile_captures:
                    scroll_click(page, confirm_profile)
                    review_profile = wait_capture(
                        page,
                        review_profile_captures,
                        lambda item: item["path"].endswith("/review"),
                        description="确认业务档案",
                    )
                if review_profile["status"] != 200:
                    raise DemoFlowError(
                        f"确认业务档案失败：HTTP {review_profile['status']} "
                        f"{review_profile.get('text', review_profile.get('json'))}"
                    )
                page.wait_for_timeout(300)
                profile = get_json(client, f"/businesses/{business_id}")
                if profile.get("review_status") != "approved":
                    raise DemoFlowError(f"业务档案未获批准：{profile.get('review_status')}")
                steps.append(
                    {
                        "name": "业务审核",
                        "status": "completed",
                        "details": f"确认 `catalog-api -> store` depends_on；角色变更 "
                        f"`{', '.join(role_changes) if role_changes else '无需修正'}`；"
                        f"profile_version=`{profile['profile_version']}`，review_status=`approved`。",
                    }
                )
                screenshots.append(shot(page, screenshot_dir, "02-business-reviewed"))

                page.get_by_role("button", name="服务器与服务", exact=False).first.click()
                page.get_by_role("heading", name="服务器与服务", exact=True).wait_for()
                page.wait_for_timeout(800)
                target_panel = page.locator("section.panel").filter(
                    has=page.get_by_role("heading", name="关联 Linux 环境", exact=True)
                ).first
                target_panel.get_by_label("环境名称", exact=True).fill("Hackathon Isolated Mock")
                connector_select = target_panel.locator('select:has(option[value="mock"])')
                expect(connector_select).to_have_value("mock")
                target_panel.locator('select[name="scenario"]').select_option("mixed")
                target_panel.locator('textarea[name="paths"]').fill("/srv/catalog")
                page.wait_for_timeout(900)
                with capture_responses(
                    page,
                    lambda response: response.request.method == "POST"
                    and api_path(response.url)
                    == f"/api/v1/businesses/{business_id}/targets",
                ) as target_captures:
                    scroll_click(
                        page,
                        target_panel.get_by_role("button", name="保存环境关联", exact=True),
                        pause_ms=0,
                    )
                    target_response = wait_capture(
                        page,
                        target_captures,
                        lambda item: item["path"].endswith("/targets"),
                        description="创建 Mock 目标",
                    )
                if target_response["status"] != 201:
                    raise DemoFlowError(
                        f"创建 Mock 目标失败：HTTP {target_response['status']} "
                        f"{target_response.get('text', target_response.get('json'))}"
                    )
                target = captured_json(target_response, description="创建 Mock 目标")
                target_id = target["id"]
                expect(page.get_by_label("运行环境", exact=True)).to_have_value(target_id)
                page.wait_for_timeout(800)

                discovery_panel = page.locator("section.panel").filter(
                    has=page.get_by_role("heading", name="轻量发现与范围审核", exact=True)
                ).first
                with capture_responses(
                    page,
                    lambda response: response.request.method == "POST"
                    and api_path(response.url)
                    == f"/api/v1/businesses/{business_id}/targets/{target_id}/discover",
                ) as discovery_captures:
                    scroll_click(
                        page,
                        discovery_panel.get_by_role("button", name="定向轻量发现", exact=True),
                        pause_ms=0,
                    )
                    discovery_response = wait_capture(
                        page,
                        discovery_captures,
                        lambda item: item["path"].endswith("/discover"),
                        description="启动范围发现",
                    )
                if discovery_response["status"] != 202:
                    raise DemoFlowError(
                        f"启动范围发现失败：HTTP {discovery_response['status']} "
                        f"{discovery_response.get('text', discovery_response.get('json'))}"
                    )
                discovery_start = captured_json(discovery_response, description="启动范围发现")
                discovery_job = wait_for_job(client, discovery_start["job_id"], args.timeout)
                catalog_row = page.get_by_test_id("binding-authorization").filter(
                    has_text="catalog-api → catalog-api.service"
                ).first
                catalog_row.wait_for()
                catalog_row.scroll_into_view_if_needed()
                page.wait_for_timeout(900)
                if "持久授权：有效" not in catalog_row.inner_text():
                    with capture_responses(
                        page,
                        lambda response: response.request.method == "POST"
                        and "/bindings/" in api_path(response.url)
                        and api_path(response.url).endswith("/review"),
                    ) as binding_captures:
                        scroll_click(
                            page,
                            catalog_row.get_by_role("button", name="确认授权", exact=True),
                            pause_ms=0,
                        )
                        binding_response = wait_capture(
                            page,
                            binding_captures,
                            lambda item: "/bindings/" in item["path"]
                            and item["path"].endswith("/review"),
                            description="确认 catalog-api 范围",
                        )
                    if binding_response["status"] != 200:
                        raise DemoFlowError(
                            f"确认 catalog-api 范围失败：HTTP {binding_response['status']} "
                            f"{binding_response.get('text', binding_response.get('json'))}"
                        )
                expect(
                    page.get_by_test_id("binding-authorization")
                    .filter(has_text="catalog-api → catalog-api.service")
                    .first
                ).to_contain_text("持久授权：有效")

                unrelated_summary = page.get_by_text(
                    "未匹配或人工判定不相关（默认折叠）", exact=True
                )
                if unrelated_summary.count():
                    scroll_click(page, unrelated_summary.first, pause_ms=900)

                defaults_summary = page.get_by_text("日常调查默认设置", exact=True)
                defaults_summary.first.scroll_into_view_if_needed()
                if not defaults_summary.first.locator("xpath=..").get_attribute("open"):
                    scroll_click(page, defaults_summary.first, pause_ms=500)
                page.get_by_label("默认服务范围", exact=True).select_option(["catalog-api"])
                page.wait_for_timeout(350)
                page.get_by_label("优先调查服务", exact=True).select_option("catalog-api")
                page.wait_for_timeout(500)
                with capture_responses(
                    page,
                    lambda response: response.request.method == "PUT"
                    and api_path(response.url)
                    == f"/api/v1/businesses/{business_id}/investigation-defaults",
                ) as defaults_captures:
                    scroll_click(
                        page,
                        page.get_by_role(
                            "button", name="将当前目标保存为调查默认", exact=True
                        ),
                        pause_ms=0,
                    )
                    defaults_response = wait_capture(
                        page,
                        defaults_captures,
                        lambda item: item["path"].endswith("/investigation-defaults"),
                        description="保存调查默认",
                    )
                if defaults_response["status"] != 200:
                    raise DemoFlowError(
                        f"保存调查默认失败：HTTP {defaults_response['status']} "
                        f"{defaults_response.get('text', defaults_response.get('json'))}"
                    )
                bindings = list_value(get_json(client, f"/businesses/{business_id}/bindings"))
                catalog_binding = next(
                    (
                        item
                        for item in bindings
                        if item.get("target_id") == target_id
                        and item.get("service") == "catalog-api"
                        and item.get("kind") == "service"
                        and item.get("authorization_valid")
                    ),
                    None,
                )
                if not catalog_binding:
                    raise DemoFlowError("未找到 catalog-api.service 的有效持久授权")
                discovery_result = discovery_job.get("result", {})
                steps.append(
                    {
                        "name": "Mock 服务器范围",
                        "status": "completed",
                        "details": f"UI 创建 target_id=`{target_id}`；discovery job="
                        f"`{discovery_start['job_id']}`，匹配 "
                        f"`{len(discovery_result.get('matches', []))}` 项；确认 binding="
                        f"`{catalog_binding['id']}` (`catalog-api -> catalog-api.service`)；"
                        f"无关对象 `{', '.join(discovery_result.get('unassigned_runtime_objects', []))}` "
                        f"保留为未授权状态；默认范围已保存为 catalog-api。",
                    }
                )
                screenshots.append(shot(page, screenshot_dir, "03-scope-confirmed"))

                page.get_by_role("button", name="排查故障", exact=False).first.click()
                page.get_by_role("heading", name="排查故障", exact=True).wait_for()
                page.wait_for_timeout(1200)
                system_select = page.locator('select[name="system_id"]')
                expect(system_select).to_have_value(system_id)
                expect(page.get_by_label("本次调查 SSH 目标", exact=True)).to_have_value(target_id)
                page.locator('textarea[name="symptom"]').fill(
                    "外部请求返回 502，应用启动检查失败"
                )
                page.locator('select[name="task_type"]').select_option(
                    "root_cause_investigation"
                )
                page.wait_for_timeout(1200)
                screenshots.append(shot(page, screenshot_dir, "04-investigation-ready"))

                if args.stop_before_run:
                    steps.append(
                        {
                            "name": "预检停止",
                            "status": "skipped",
                            "details": "已按 --stop-before-run 在提交调查前停止；未创建事件或 Run。",
                        }
                    )
                    raise StopBeforeRun

                with capture_responses(
                    page,
                    lambda response: response.request.method == "POST"
                    and (
                        api_path(response.url) == "/api/v1/incidents"
                        or api_path(response.url).endswith("/runs")
                    ),
                ) as run_captures:
                    scroll_click(
                        page,
                        page.get_by_role(
                            "button", name="提交故障并开始调查", exact=False
                        ).first,
                        pause_ms=700,
                    )
                    incident_response = wait_capture(
                        page,
                        run_captures,
                        lambda item: item["path"] == "/api/v1/incidents",
                        description="创建故障事件",
                    )
                    run_response = wait_capture(
                        page,
                        run_captures,
                        lambda item: item["path"].endswith("/runs"),
                        description="启动故障调查",
                    )
                if incident_response["status"] not in {200, 201}:
                    raise DemoFlowError(
                        f"创建事件失败：HTTP {incident_response['status']} "
                        f"{incident_response.get('text', incident_response.get('json'))}"
                    )
                if run_response["status"] != 202:
                    raise DemoFlowError(
                        f"启动调查失败：HTTP {run_response['status']} "
                        f"{run_response.get('text', run_response.get('json'))}"
                    )
                incident_id = ""
                run_id = ""
                try:
                    incident_id = captured_json(
                        incident_response, description="创建故障事件"
                    )["id"]
                except DemoFlowError:
                    pass
                try:
                    run_id = captured_json(
                        run_response, description="启动故障调查"
                    )["run_id"]
                except DemoFlowError:
                    pass
                if not incident_id or not run_id:
                    incident = wait_for_incident_run(client, system_id, args.timeout)
                    incident_id = incident_id or incident["id"]
                    run_id = run_id or incident["latest_run_id"]
                page.locator(".run-view").wait_for()
                page.wait_for_timeout(900)

                final_run = wait_for_run(client, run_id, args.timeout)
                expect(page.locator(".run-view")).to_contain_text(
                    "执行状态：已结束", timeout=args.timeout * 1000
                )
                conclusion = page.get_by_test_id("investigation-conclusion")
                expect(conclusion).to_be_visible(timeout=30_000)
                conclusion.scroll_into_view_if_needed()
                page.wait_for_timeout(1_600)

                audit = page.get_by_test_id("evidence-audit")
                audit.scroll_into_view_if_needed()
                if not audit.get_attribute("open"):
                    scroll_click(page, audit.locator("summary").first, pause_ms=700)
                expect(page.get_by_test_id("ab-fact-report")).to_be_visible(timeout=30_000)
                missing = page.get_by_text("缺失证据", exact=False).first
                if missing.count():
                    missing.scroll_into_view_if_needed()
                    page.wait_for_timeout(1_200)
                conclusion.scroll_into_view_if_needed()
                page.wait_for_timeout(4_500)
                screenshots.append(shot(page, screenshot_dir, "05-final-evidence-gate"))

                diagnosis = final_run.get("diagnosis_status") or (
                    final_run.get("diagnosis_result") or {}
                ).get("diagnosis_status")
                steps.append(
                    {
                        "name": "故障调查",
                        "status": "completed",
                        "details": f"UI 创建 incident_id=`{incident_id}`，并以 "
                        f"root_cause_investigation 启动 run_id=`{run_id}`；"
                        f"execution_status=`{final_run.get('execution_status')}`，"
                        f"diagnosis_status=`{diagnosis}`。",
                    }
                )
                if page_errors:
                    steps.append(
                        {
                            "name": "浏览器错误",
                            "status": "warning",
                            "details": "；".join(page_errors),
                        }
                    )
                if http_errors:
                    steps.append(
                        {
                            "name": "HTTP 错误",
                            "status": "warning",
                            "details": "；".join(http_errors),
                        }
                    )

                context.close()
                context = None
                browser.close()
                browser = None
        except StopBeforeRun:
            pass
        except Exception:
            flow_error = traceback.format_exc()

        video_source: Path | None = None
        if video is not None:
            try:
                if context is not None:
                    context.close()
                    context = None
                if browser is not None:
                    browser.close()
                    browser = None
                video_source = Path(video.path())
            except Exception:
                if flow_error:
                    flow_error += "\n" + traceback.format_exc()
                else:
                    flow_error = traceback.format_exc()

        if video_source and video_source.is_file():
            final_video.unlink(missing_ok=True)
            shutil.move(str(video_source), str(final_video))
            video_text = str(final_video)
        elif final_video.is_file():
            video_text = str(final_video)
        else:
            video_text = ""

        if playwright_session is not None:
            playwright_session.stop()

        if run_id and not final_run:
            try:
                final_run = get_json(client, f"/runs/{run_id}")
            except Exception:
                pass

        write_report(
            report_path,
            base_url=base_url,
            started_at=started_at,
            finished_at=now_iso(),
            fixture=fixture,
            business_name=business_name,
            steps=steps,
            prior_runs=prior_runs,
            run=final_run,
            error=flow_error,
            video_path=video_text,
            screenshots=screenshots,
        )
        print(f"Report: {report_path}")
        print(f"Video: {video_text or 'not generated'}")
        if flow_error:
            print(flow_error, file=sys.stderr)
            return 1
        diagnosis = final_run.get("diagnosis_status") if final_run else None
        print(f"Demo completed; diagnosis_status={diagnosis}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
