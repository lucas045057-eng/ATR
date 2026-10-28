from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_VIEWPORT = {"width": 1600, "height": 900}
SHOWCASE_SYMPTOM = "catalog-api 服务不可用，外部请求返回 502"

CAPTION_STEPS = [
    (
        "01 / 06 · UNDERSTAND",
        "先理解系统，再开始排障",
        "上传陌生源码，ATR 静态解析并自动建立可溯源的业务图谱与画像",
    ),
    (
        "02 / 06 · HUMAN REVIEW",
        "关键关系和调查范围由人确认",
        "确认 catalog-api → store 依赖与授权对象；边界只有一句：全程只读，模型不修复",
    ),
    (
        "03 / 06 · INVESTIGATE",
        "让证据决定下一步检查",
        "Runtime 把检查组织成可验证动作链，每一步都回答一个明确问题",
    ),
    (
        "04 / 06 · VERIFY",
        "确定性证据门只认当前证据",
        "事实、范围、冲突与缺口分别核验，结论等级不能被模型自信度升级",
    ),
    (
        "05 / 06 · RESULT",
        "一次真实收敛的调查",
        "直接故障已定位；触发原因仍明确保持未知，边界没有被叙事夸大",
    ),
    (
        "06 / 06 · LEARN",
        "经验必须经过验证才能复用",
        "人工修复与恢复验证后才形成待审核 Pattern；复用时仍要重新取证",
    ),
]

NARRATION_LINES = [
    "面对陌生业务，ATR 先从资料建立系统认知。上传源码后，静态解析提取服务、角色和依赖，并为每一条关系保留来源。",
    "关键关系由人确认，调查范围只覆盖真正相关的对象。全程只读，模型不执行修复。",
    "故障出现后，Runtime 把可用动作组织成可验证的检查链。每一步都回答一个问题，并用当前证据决定下一步。",
    "检查结束后，确定性证据门核对事实、范围与缺口，只允许当前证据支持对应的结论等级。",
    "这次调查调用三次只读工具，模型调用零次，完成直接故障定位；触发原因仍明确保持未知。",
    "验证后的经验才能沉淀为可信 Pattern，复用时仍要重新验证当前证据。理解，调查，验证，学习。",
]


def load_final_recorder():
    path = ROOT / "scripts" / "demo_record_final.py"
    spec = importlib.util.spec_from_file_location("atr_demo_record_final", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load recorder helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load_final_recorder()


OVERLAY_BOOTSTRAP = r"""
(() => {
  const install = () => {
    if (!document.body || document.getElementById("atr-showcase-caption")) return;

    const style = document.createElement("style");
    style.id = "atr-showcase-caption-style";
    style.textContent = `
      #atr-showcase-caption {
        position: fixed;
        left: 50%;
        bottom: 28px;
        z-index: 2147483647;
        width: min(960px, calc(100vw - 96px));
        box-sizing: border-box;
        padding: 15px 22px 16px;
        border: 1px solid rgba(255, 255, 255, 0.15);
        border-radius: 16px;
        background: rgba(6, 14, 20, 0.86);
        box-shadow: 0 18px 60px rgba(0, 0, 0, 0.34);
        color: #ffffff;
        font-family: "Microsoft YaHei UI", "Noto Sans SC", sans-serif;
        text-align: center;
        pointer-events: none;
        opacity: 0;
        transform: translate(-50%, 14px);
        transition: opacity 280ms ease, transform 280ms ease;
        backdrop-filter: blur(10px);
      }
      #atr-showcase-caption.is-visible {
        opacity: 1;
        transform: translate(-50%, 0);
      }
      #atr-showcase-caption .step {
        margin-bottom: 5px;
        color: #efb64b;
        font: 700 12px/1.2 "Cascadia Mono", Consolas, monospace;
        letter-spacing: 0.18em;
        text-transform: uppercase;
      }
      #atr-showcase-caption .title {
        margin-bottom: 4px;
        font-size: 18px;
        font-weight: 700;
        letter-spacing: 0.02em;
      }
      #atr-showcase-caption .text {
        color: rgba(255, 255, 255, 0.93);
        font-size: 21px;
        line-height: 1.45;
        letter-spacing: 0.01em;
      }
      @media (max-width: 1100px) {
        #atr-showcase-caption {
          bottom: 20px;
          width: calc(100vw - 48px);
          padding: 13px 18px 14px;
        }
        #atr-showcase-caption .text { font-size: 18px; }
      }
    `;
    document.head.appendChild(style);

    const caption = document.createElement("div");
    caption.id = "atr-showcase-caption";
    caption.setAttribute("aria-live", "polite");
    caption.innerHTML = `
      <div class="step"></div>
      <div class="title"></div>
      <div class="text"></div>
    `;
    document.body.appendChild(caption);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install, { once: true });
  } else {
    install();
  }
})();
"""


INTRO_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  :root {
    --ink: #eef4f4;
    --muted: #9fb4bb;
    --gold: #efb64b;
    --teal: #45d6c7;
  }
  * { box-sizing: border-box; }
  html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; }
  body {
    color: var(--ink);
    background:
      radial-gradient(circle at 78% 18%, rgba(69, 214, 199, 0.16), transparent 34%),
      radial-gradient(circle at 18% 84%, rgba(239, 182, 75, 0.14), transparent 34%),
      linear-gradient(135deg, #07131b 0%, #0c1e29 52%, #112b35 100%);
    font-family: "Microsoft YaHei UI", "Noto Sans SC", sans-serif;
  }
  body::before {
    content: "";
    position: fixed;
    inset: 0;
    background-image:
      linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
    background-size: 48px 48px;
    mask-image: linear-gradient(to bottom, rgba(0,0,0,.7), transparent 92%);
  }
  main {
    position: relative;
    min-height: 100%;
    display: grid;
    align-content: center;
    padding: 96px 112px 110px;
  }
  .brand {
    display: inline-flex;
    align-items: center;
    gap: 12px;
    width: fit-content;
    margin-bottom: 44px;
    color: var(--gold);
    font: 700 13px/1 "Cascadia Mono", Consolas, monospace;
    letter-spacing: .24em;
  }
  .brand-mark {
    display: grid;
    place-items: center;
    width: 38px;
    height: 38px;
    border: 1px solid rgba(239,182,75,.65);
    border-radius: 10px;
    background: rgba(239,182,75,.08);
    color: var(--gold);
    font-size: 15px;
    letter-spacing: -.04em;
  }
  h1 {
    max-width: 1120px;
    margin: 0 0 24px;
    font: 700 clamp(48px, 5.3vw, 82px)/1.05 "Microsoft YaHei UI", "Noto Sans SC", sans-serif;
    letter-spacing: -.035em;
  }
  h1 span { color: var(--gold); }
  .subtitle {
    margin: 0;
    color: var(--muted);
    font-size: clamp(21px, 1.8vw, 30px);
    letter-spacing: .06em;
  }
  .process {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-top: 42px;
    color: rgba(238,244,244,.82);
    font: 700 14px/1 "Cascadia Mono", Consolas, monospace;
    letter-spacing: .08em;
  }
  .process span {
    padding: 10px 14px;
    border: 1px solid rgba(255,255,255,.16);
    border-radius: 999px;
    background: rgba(255,255,255,.035);
  }
  .process b { color: var(--teal); font-weight: 700; }
  .accent {
    width: 136px;
    height: 5px;
    margin-top: 34px;
    background: linear-gradient(90deg, var(--gold), var(--teal));
    border-radius: 999px;
    box-shadow: 0 0 34px rgba(69,214,199,.2);
  }
  .footer {
    position: absolute;
    left: 112px;
    bottom: 54px;
    color: rgba(238,244,244,.48);
    font: 600 12px/1.5 "Cascadia Mono", Consolas, monospace;
    letter-spacing: .16em;
  }
</style>
</head>
<body>
  <main>
    <div class="brand"><span class="brand-mark">ATR</span> ADAPTIVE TROUBLESHOOTING RUNTIME</div>
    <h1>ATR <span>·</span> Adaptive<br>Troubleshooting Runtime</h1>
    <p class="subtitle">面向复杂、陌生和异构业务环境的 AI 自主故障调查运行时</p>
    <div class="process">
      <span>Understand</span><b>→</b><span>Investigate</span><b>→</b><span>Verify</span><b>→</b><span>Learn</span>
    </div>
    <div class="accent"></div>
  </main>
  <div class="footer">SYSTEM-AWARE · EVIDENCE-DRIVEN · ADAPTIVE INVESTIGATION</div>
</body>
</html>
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_mark(
    marks: dict[str, Any],
    started: float,
    kind: str,
    name: str,
    **details: Any,
) -> float:
    item = {
        "kind": kind,
        "name": name,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "timestamp": now_iso(),
        **details,
    }
    marks["events"].append(item)
    if kind == "caption":
        marks["captions"].append(item)
    return float(item["elapsed_seconds"])


def slow_click(page: Page, locator, *, before_ms: int = 1000, after_ms: int = 1300) -> None:
    locator.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    locator.hover()
    page.wait_for_timeout(before_ms)
    locator.click()
    if after_ms:
        page.wait_for_timeout(after_ms)


def slow_fill(page: Page, locator, value: str, *, delay_ms: int = 45) -> None:
    locator.scroll_into_view_if_needed()
    locator.click()
    page.wait_for_timeout(450)
    locator.press("Control+A")
    locator.press("Backspace")
    if delay_ms <= 0:
        locator.fill(value)
    else:
        locator.press_sequentially(value, delay=delay_ms)
    page.wait_for_timeout(900)


def slow_select(page: Page, locator, value: str | list[str]) -> None:
    locator.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    locator.hover()
    page.wait_for_timeout(850)
    locator.select_option(value)
    page.wait_for_timeout(1100)


def ensure_dark_theme(page: Page) -> None:
    if page.evaluate("document.documentElement.dataset.theme") == "dark":
        return
    theme_button = page.get_by_role("button", name="切换亮暗主题", exact=True)
    theme_button.click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.wait_for_timeout(700)


def open_structure_page(
    page: Page, screenshot_dir: Path, expected_system_id: str
) -> None:
    structure_nav = page.locator("nav").get_by_role(
        "button", name="业务结构图", exact=False
    )
    slow_click(page, structure_nav, before_ms=850, after_ms=900)
    try:
        expect(page.locator(".breadcrumb")).to_contain_text(
            "业务结构图", timeout=20_000
        )
        business_select = page.get_by_label("选择图的业务", exact=True)
        business_select.wait_for(timeout=20_000)
        option_values = business_select.locator("option").evaluate_all(
            "options => options.map(option => option.value)"
        )
        if expected_system_id not in option_values:
            page.get_by_title("刷新", exact=True).click()
            page.wait_for_function(
                """(expected) => Array.from(
                    document.querySelectorAll('select[aria-label="选择图的业务"] option')
                ).some(option => option.value === expected)""",
                arg=expected_system_id,
                timeout=30_000,
            )
        if business_select.input_value() != expected_system_id:
            slow_select(page, business_select, expected_system_id)
        page.locator("section.structure-panel").wait_for(timeout=30_000)
    except Exception as error:
        failure_path = screenshot_dir / "story-structure-navigation-failed.png"
        page.screenshot(path=str(failure_path), full_page=True)
        breadcrumb = page.locator(".breadcrumb").inner_text()
        body = page.locator("body").inner_text()[:3000]
        raise RuntimeError(
            "业务结构图导航未完成。"
            f"\nbreadcrumb={breadcrumb!r}"
            f"\nbody={body!r}"
            f"\nscreenshot={failure_path}"
        ) from error


def set_caption(page: Page, index: int, marks: dict[str, Any], started: float) -> None:
    step, title, text = CAPTION_STEPS[index - 1]
    visible = page.evaluate(
        """({step, title, text}) => {
            const el = document.getElementById("atr-showcase-caption");
            if (!el) return false;
            el.classList.remove("is-visible");
            void el.offsetWidth;
            el.querySelector(".step").textContent = step;
            el.querySelector(".title").textContent = title;
            el.querySelector(".text").textContent = text;
            el.classList.add("is-visible");
            return true;
        }""",
        {"step": step, "title": title, "text": text},
    )
    if not visible:
        raise RuntimeError("Caption overlay was not installed before the UI navigation")
    add_mark(
        marks,
        started,
        "caption",
        f"caption_{index}",
        caption={"step": step, "title": title, "text": text},
    )


def hide_caption(page: Page) -> None:
    page.evaluate(
        """() => {
            const el = document.getElementById("atr-showcase-caption");
            if (el) el.classList.remove("is-visible");
        }"""
    )


def remove_overlays(page: Page) -> None:
    page.evaluate(
        """() => {
            for (const id of [
              "atr-investigation-ledger",
              "atr-result-card",
              "atr-learning-card",
              "atr-showcase-endcard"
            ]) document.getElementById(id)?.remove();
        }"""
    )


def show_investigation_ledger(page: Page, run: dict[str, Any]) -> None:
    actions = ((run.get("summary") or {}).get("action_history") or [])[:3]
    if len(actions) != 3:
        raise RuntimeError(
            f"Expected three Runtime actions for the storyboard, got {len(actions)}"
        )
    cards = []
    for index, action in enumerate(actions, start=1):
        distinctions = " · ".join(str(item) for item in action.get("distinguishes") or [])
        new_information = bool(action.get("new_information"))
        cards.append(
            {
                "index": f"{index:02d}",
                "tool": action.get("tool_id") or "unknown",
                "question": action.get("question") or "Runtime 未提供问题字段",
                "judgment": action.get("changes_judgment") or "Runtime 未提供判据字段",
                "distinctions": distinctions or "未提供可区分状态",
                "newInformation": "带来新信息" if new_information else "身份核验通过",
                "tone": "#72e2b6" if new_information else "#efb64b",
            }
        )
    page.evaluate(
        """(cards) => {
          document.getElementById("atr-investigation-ledger")?.remove();
          const root = document.createElement("div");
          root.id = "atr-investigation-ledger";
          root.style.cssText = `
            position: fixed;
            left: 50%;
            bottom: 124px;
            transform: translateX(-50%);
            z-index: 2147483645;
            width: min(1360px, calc(100vw - 72px));
            box-sizing: border-box;
            padding: 18px 20px 20px;
            border: 1px solid rgba(255,255,255,.16);
            border-radius: 18px;
            color: #eef4f4;
            background: linear-gradient(145deg, rgba(6,18,25,.96), rgba(14,37,46,.94));
            box-shadow: 0 24px 90px rgba(0,0,0,.5);
            font-family: "Microsoft YaHei UI", "Noto Sans SC", sans-serif;
            backdrop-filter: blur(16px);
            pointer-events: none;
          `;
          root.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
              <div>
                <div style="color:#efb64b;font:700 11px/1.2 'Cascadia Mono',Consolas,monospace;letter-spacing:.18em">RUNTIME ACTION LEDGER</div>
                <div style="margin-top:7px;font-size:19px;font-weight:700">检查链来自真实 Run 字段：每一步都先定义问题，再决定证据是否改变判断</div>
              </div>
              <div style="color:#95abb3;font:600 11px/1.4 'Cascadia Mono',Consolas,monospace">summary.action_history</div>
            </div>
            <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px">
              ${cards.map((card) => `
                <article style="border:1px solid rgba(255,255,255,.13);border-radius:13px;background:rgba(255,255,255,.045);padding:14px 15px;min-height:176px">
                  <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
                    <span style="display:grid;place-items:center;width:30px;height:30px;border-radius:9px;color:${card.tone};background:rgba(255,255,255,.06);font:700 11px 'Cascadia Mono',Consolas,monospace">${card.index}</span>
                    <strong style="font:700 14px 'Cascadia Mono',Consolas,monospace;color:${card.tone}">${card.tool}</strong>
                    <span style="margin-left:auto;color:#9db0b7;font-size:11px">${card.newInformation}</span>
                  </div>
                  <div style="color:#f5f8f8;font-size:17px;line-height:1.45;font-weight:650">${card.question}</div>
                  <div style="margin-top:11px;color:#a9bbc1;font-size:13px;line-height:1.5">判据：${card.judgment}</div>
                  <div style="margin-top:9px;color:#72e2b6;font-size:12px;line-height:1.5">区分：${card.distinctions}</div>
                </article>
              `).join("")}
            </div>
          `;
          document.body.appendChild(root);
        }""",
        cards,
    )


def show_result_card(page: Page, run: dict[str, Any], usage: dict[str, Any]) -> None:
    hide_caption(page)
    diagnosis_result = run.get("diagnosis_result") or {}
    findings = (
        diagnosis_result.get("direct_findings")
        or diagnosis_result.get("direct_faults")
        or []
    )
    required_gaps = diagnosis_result.get("required_gaps") or []
    direct_explanations = diagnosis_result.get("direct_explanations") or []
    first_explanation = (
        direct_explanations[0]
        if direct_explanations and isinstance(direct_explanations[0], dict)
        else {}
    )
    payload = {
        "runId": str(run.get("id") or "")[:8],
        "diagnosis": str(run.get("diagnosis_status") or "unknown"),
        "tools": int((usage or {}).get("tool_calls") or 0),
        "models": int((usage or {}).get("model_calls") or 0),
        "facts": len(findings),
        "gaps": len(required_gaps),
        "complete": bool(run.get("completion_satisfied")),
        "trigger": str(first_explanation.get("trigger_cause_status") or "unknown"),
    }
    page.evaluate(
        """(data) => {
          document.getElementById("atr-result-card")?.remove();
          const card = document.createElement("div");
          card.id = "atr-result-card";
          card.style.cssText = `
            position: fixed; inset: 0; z-index: 2147483646;
            display: grid; place-content: center; padding: 72px 110px;
            box-sizing: border-box; text-align: center; color: #eef4f4;
            font-family: "Microsoft YaHei UI", "Noto Sans SC", sans-serif;
            background:
              radial-gradient(circle at 50% 24%, rgba(69,214,199,.14), transparent 32%),
              linear-gradient(145deg, #06131b 0%, #0b2029 58%, #102a34 100%);
          `;
          const metrics = [
            [data.tools, "只读工具调用"],
            [data.models, "模型调用"],
            [data.facts, "独立当前事实"],
            [data.gaps, "必须补齐的证据"],
          ];
          card.innerHTML = `
            <div style="color:#efb64b;font:700 12px/1.2 'Cascadia Mono',Consolas,monospace;letter-spacing:.22em;margin-bottom:20px">EVIDENCE-GATED OUTCOME · RUN ${data.runId}</div>
            <div style="font:700 clamp(44px,5vw,74px)/1.05 'Cascadia Mono',Consolas,monospace;color:#72e2b6;letter-spacing:-.045em">${data.diagnosis}</div>
            <div style="margin-top:18px;color:#f1f7f7;font-size:27px;font-weight:650">直接故障已定位，触发原因保持未知</div>
            <div style="margin-top:12px;color:#9fb4bb;font-size:18px">completion_satisfied = ${data.complete} · trigger_cause_status = ${data.trigger}</div>
            <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-top:42px;min-width:940px">
              ${metrics.map(([value,label]) => `
                <div style="padding:20px 16px;border:1px solid rgba(255,255,255,.12);border-radius:14px;background:rgba(255,255,255,.04)">
                  <div style="font:700 36px/1 'Cascadia Mono',Consolas,monospace;color:#eef4f4">${value}</div>
                  <div style="margin-top:9px;color:#9fb4bb;font-size:14px">${label}</div>
                </div>
              `).join("")}
            </div>
            <div style="margin-top:38px;color:#efb64b;font-size:20px;letter-spacing:.035em">调查完成不等于根因确认：结论边界由证据门决定</div>
          `;
          document.body.appendChild(card);
        }""",
        payload,
    )


def show_learning_card(page: Page) -> None:
    page.evaluate(
        """() => {
          document.getElementById("atr-learning-card")?.remove();
          const card = document.createElement("div");
          card.id = "atr-learning-card";
          card.style.cssText = `
            position: fixed; left: 50%; bottom: 130px; transform: translateX(-50%);
            z-index: 2147483645; width: min(1120px, calc(100vw - 96px));
            box-sizing: border-box; padding: 20px 24px; border-radius: 18px;
            border: 1px solid rgba(255,255,255,.15); color: #eef4f4;
            background: rgba(5,17,24,.93); box-shadow: 0 24px 90px rgba(0,0,0,.48);
            font-family: "Microsoft YaHei UI", "Noto Sans SC", sans-serif;
            backdrop-filter: blur(16px); pointer-events: none;
          `;
          card.innerHTML = `
            <div style="color:#efb64b;font:700 11px/1.2 'Cascadia Mono',Consolas,monospace;letter-spacing:.18em">LEARNING LOOP</div>
            <div style="display:flex;align-items:center;gap:12px;margin-top:13px;color:#f3f8f8;font-size:18px;font-weight:650">
              <span>调查记录</span><b style="color:#72e2b6">→</b><span>人工修复</span><b style="color:#72e2b6">→</b><span>恢复验证</span><b style="color:#72e2b6">→</b><span>候选 Pattern</span><b style="color:#72e2b6">→</b><span>人工审核</span>
            </div>
            <div style="margin-top:13px;color:#a7bac1;font-size:14px;line-height:1.55">本 demo 使用直接故障定位，未执行人工修复与恢复验证，因此不会自动生成 Trusted Pattern。知识页保持真实空态，而不是用历史答案替代当前证据。</div>
          `;
          document.body.appendChild(card);
        }"""
    )


def show_end_card(
    page: Page,
    *,
    dry_run: bool,
    run: dict[str, Any] | None,
    usage: dict[str, Any] | None,
) -> None:
    hide_caption(page)
    if dry_run:
        kicker = "PREFLIGHT COMPLETE"
        headline = "全流程预演完成"
        status_line = "未提交调查 · 未创建新的 Run"
        metrics = [
            "UI 全流程：已走通",
            "正式 Run：0",
            "本次用途：画面与节奏预演",
        ]
        slogan = "下一步：在同一配置下完成正式录屏"
    else:
        kicker = "ATR / ADAPTIVE TROUBLESHOOTING RUNTIME"
        headline = "Understand → Investigate → Verify → Learn"
        status_line = "从陌生业务理解，到有证据约束的自主调查，再到可审核的经验沉淀"
        metrics = [
            "Understand · 自动建立可溯源的业务图谱",
            "Investigate · Runtime 规划有界只读检查链",
            "Verify · 确定性证据门约束结论等级",
            "Learn · 人工验证后再形成可信经验",
        ]
        slogan = "不做会瞎修的 AI 助手，做有证据约束的故障调查运行时"

    payload = {
        "kicker": kicker,
        "headline": headline,
        "statusLine": status_line,
        "metrics": metrics,
        "slogan": slogan,
    }
    page.evaluate(
        """({kicker, headline, statusLine, metrics, slogan}) => {
            const old = document.getElementById("atr-showcase-endcard");
            if (old) old.remove();
            const card = document.createElement("div");
            card.id = "atr-showcase-endcard";
            card.style.cssText = `
                position: fixed;
                inset: 0;
                z-index: 2147483646;
                display: grid;
                place-content: center;
                padding: 84px 120px;
                box-sizing: border-box;
                color: #eef4f4;
                text-align: center;
                font-family: "Microsoft YaHei UI", "Noto Sans SC", sans-serif;
                background:
                  radial-gradient(circle at 50% 30%, rgba(69,214,199,.15), transparent 34%),
                  radial-gradient(circle at 20% 82%, rgba(239,182,75,.12), transparent 28%),
                  linear-gradient(145deg, #07131b 0%, #0b2029 55%, #102a34 100%);
            `;
            card.innerHTML = `
              <div style="color:#efb64b;font:700 13px/1.2 'Cascadia Mono',Consolas,monospace;letter-spacing:.22em;margin-bottom:28px">${kicker}</div>
              <div style="font:700 clamp(38px,4.4vw,68px)/1.08 'Cascadia Mono',Consolas,monospace;color:#72e2b6;letter-spacing:-.04em">${headline}</div>
              <div style="margin-top:18px;color:#b9cbd0;font-size:21px;letter-spacing:.035em">${statusLine}</div>
              <div style="display:grid;gap:12px;margin:44px auto 0;min-width:620px">
                ${metrics.map((x, index) => `<div style="display:grid;grid-template-columns:44px 1fr;gap:14px;align-items:center;padding:13px 20px;border:1px solid rgba(255,255,255,.12);border-radius:12px;background:rgba(255,255,255,.035);color:#e8f0f1;font:600 18px/1.4 'Microsoft YaHei UI','Noto Sans SC',sans-serif"><b style="font:700 11px 'Cascadia Mono',Consolas,monospace;color:#efb64b">0${index+1}</b><span style="text-align:left">${x}</span></div>`).join("")}
              </div>
              <div style="margin-top:50px;color:#efb64b;font-size:22px;letter-spacing:.035em">${slogan}</div>
            `;
            document.body.appendChild(card);
        }""",
        payload,
    )


def review_binding(
    page: Page,
    *,
    service: str,
    kind: str,
    decision: str,
    description: str,
) -> dict[str, Any]:
    row = (
        page.get_by_test_id("binding-authorization")
        .filter(has_text=f"{service} →")
        .filter(has_text=kind)
        .first
    )
    row.wait_for()
    row.scroll_into_view_if_needed()
    page.wait_for_timeout(900)
    if decision == "confirm" and "持久授权：有效" in row.inner_text():
        return {}

    button_name = "确认授权" if decision == "confirm" else "拒绝"
    button = row.get_by_role("button", name=button_name, exact=True)
    with legacy.capture_responses(
        page,
        lambda response: response.request.method == "POST"
        and "/bindings/" in legacy.api_path(response.url)
        and legacy.api_path(response.url).endswith("/review"),
    ) as captures:
        slow_click(page, button, before_ms=1150, after_ms=0)
        response = legacy.wait_capture(
            page,
            captures,
            lambda item: "/bindings/" in item["path"] and item["path"].endswith("/review"),
            description=description,
        )
    if response["status"] != 200:
        raise RuntimeError(
            f"{description} failed: HTTP {response['status']} "
            f"{response.get('text', response.get('json'))}"
        )
    payload = legacy.captured_json(response, description=description)
    page.wait_for_timeout(1300)
    return payload


def write_showcase_report(
    path: Path,
    *,
    started_at: str,
    finished_at: str,
    dry_run: bool,
    error: str | None,
    marks: dict[str, Any],
    video_path: Path | None,
    screenshots: list[str],
    run: dict[str, Any] | None,
    capture_name: str,
) -> None:
    result = legacy.compact_run(run) if run else None
    lines = [
        "# ATR Story Recording Verification",
        "",
        f"- Recorded at: `{started_at}` -> `{finished_at}`",
        f"- Mode: `{'dry-run' if dry_run else 'formal-run'}`",
        f"- Capture: `{capture_name}`",
        f"- Video source: `{video_path or 'not generated'}`",
        f"- Status: `{'completed' if error is None else 'failed'}`",
        "",
        "## Investigation Result",
        "",
    ]
    if result:
        lines.extend(
            [
                f"- Run ID: `{result.get('id')}`",
                f"- Execution: `{result.get('execution_status')}`",
                f"- Diagnosis: `{result.get('diagnosis_status')}`",
                f"- Goal: `{result.get('investigation_goal')}`",
                f"- completion_satisfied: `{result.get('completion_satisfied')}`",
                f"- Model calls: `{result.get('usage', {}).get('model_calls')}`",
                f"- Tool calls: `{result.get('usage', {}).get('tool_calls')}`",
                f"- Tools: `{', '.join(item.get('tool_id', '') for item in result.get('tool_calls', []))}`",
                "",
            ]
        )
    else:
        lines.extend(["- No investigation Run was submitted.", ""])

    lines.extend(
        [
            "## Recording Marks",
            "",
            "```json",
            json.dumps(marks, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    if screenshots:
        lines.extend(["## Screenshots", ""])
        lines.extend(f"- `{item}`" for item in screenshots)
        lines.append("")
    if error:
        lines.extend(["## Error", "", "```text", error, "```", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record the ATR Understand-Investigate-Verify-Learn story."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--fixture", type=Path, default=legacy.DEFAULT_FIXTURE)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output")
    parser.add_argument("--business-name", default="")
    parser.add_argument("--symptom", default=SHOWCASE_SYMPTOM)
    parser.add_argument("--browser-channel", default="chrome")
    parser.add_argument("--timeout", type=float, default=360.0)
    parser.add_argument("--capture-name", default="atr-story-capture.webm")
    parser.add_argument(
        "--stop-before-run",
        action="store_true",
        help="Exercise the complete setup UI without creating an incident or Run.",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    api = legacy.api_base(base_url)
    fixture = args.fixture.resolve()
    output_dir = args.output_dir.resolve()
    screenshot_dir = output_dir / "screenshots"
    video_dir = output_dir / "videos"
    capture_path = output_dir / args.capture_name
    report_path = output_dir / "story-verification.md"
    marks_path = output_dir / "story-marks.json"
    run_json_path = output_dir / "story-run.json"
    narration_script_path = output_dir / "story-narration-script.json"

    for directory in (output_dir, screenshot_dir, video_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if not fixture.is_file():
        raise SystemExit(f"Fixture not found: {fixture}")

    business_name = args.business_name.strip() or (
        "ATR Story Demo " + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    started_at = now_iso()
    started_monotonic = time.monotonic()
    marks: dict[str, Any] = {
        "schema_version": 1,
        "created_at": started_at,
        "base_url": base_url,
        "viewport": DEFAULT_VIEWPORT,
        "business_name": business_name,
        "dry_run": args.stop_before_run,
        "narration_lines": NARRATION_LINES,
        "events": [],
        "captions": [],
    }
    screenshots: list[str] = []
    steps: list[dict[str, Any]] = []
    final_run: dict[str, Any] | None = None
    flow_error: str | None = None
    final_usage: dict[str, Any] | None = None
    business_id = ""
    system_id = ""
    target_id = ""
    incident_id = ""
    run_id = ""
    video_source: Path | None = None

    with httpx.Client(base_url=api, timeout=30.0) as client:
        health = legacy.get_json(client, "/health")
        if health.get("status") != "ready":
            raise SystemExit(f"ATR API is not ready: {health}")
        prior_summaries = legacy.list_value(legacy.get_json(client, "/runs"))
        marks["prior_run_count"] = len(prior_summaries)
        print(f"API ready; prior runs: {len(prior_summaries)}")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                channel=args.browser_channel or None,
            )
            context = browser.new_context(
                viewport=DEFAULT_VIEWPORT,
                device_scale_factor=1,
                record_video_dir=str(video_dir),
                record_video_size=DEFAULT_VIEWPORT,
            )
            context.add_init_script(OVERLAY_BOOTSTRAP)
            page = context.new_page()
            video = page.video
            page.set_default_timeout(25_000)
            page.set_default_navigation_timeout(45_000)
            page_errors: list[str] = []
            http_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "response",
                lambda response: http_errors.append(
                    f"{response.request.method} {legacy.api_path(response.url)} -> {response.status}"
                )
                if "/api/v1/" in response.url and response.status >= 400
                else None,
            )

            try:
                page.set_content(INTRO_HTML, wait_until="domcontentloaded")
                add_mark(marks, started_monotonic, "card", "intro_started")
                page.wait_for_timeout(5_800)

                page.goto(base_url, wait_until="domcontentloaded")
                page.get_by_role("heading", name="业务概览", exact=True).wait_for()
                ensure_dark_theme(page)
                page.wait_for_timeout(1_300)

                set_caption(page, 1, marks, started_monotonic)
                page.wait_for_timeout(1_400)
                slow_click(
                    page,
                    page.get_by_role("button", name="导入业务资料", exact=False).first,
                    before_ms=1_100,
                    after_ms=1_350,
                )
                page.locator("h1").filter(has_text="导入业务资料").wait_for()
                page.wait_for_timeout(1_000)

                create_panel = page.locator("section.panel").filter(
                    has=page.get_by_role("heading", name="创建业务概览", exact=True)
                ).first
                slow_fill(
                    page,
                    create_panel.locator('input[name="name"]'),
                    business_name,
                    delay_ms=18,
                )
                slow_fill(
                    page,
                    create_panel.locator('textarea[name="description"]'),
                    "catalog-api depends on store; isolated hackathon read-only demo.",
                    delay_ms=8,
                )

                with legacy.capture_responses(
                    page,
                    lambda response: legacy.response_is(
                        response, "POST", legacy.api_path(api + "/businesses")
                    ),
                ) as create_captures:
                    slow_click(
                        page,
                        create_panel.get_by_role("button", name="创建并保存"),
                        before_ms=1_100,
                        after_ms=0,
                    )
                    created_response = legacy.wait_capture(
                        page,
                        create_captures,
                        lambda item: item["path"] == "/api/v1/businesses",
                        description="创建业务",
                    )
                if created_response["status"] not in {200, 201}:
                    raise RuntimeError(
                        f"创建业务失败：HTTP {created_response['status']} "
                        f"{created_response.get('text', created_response.get('json'))}"
                    )
                created = legacy.captured_json(created_response, description="创建业务")
                business_id = created["business_id"]
                system_id = created["system_id"]
                marks["business_id"] = business_id
                marks["system_id"] = system_id
                expect(page.get_by_label("选择业务", exact=True)).to_have_value(business_id)
                page.wait_for_timeout(1_300)
                steps.append(
                    {
                        "name": "business-created",
                        "status": "completed",
                        "business_id": business_id,
                        "system_id": system_id,
                    }
                )

                upload_panel = page.locator("section.panel").filter(
                    has=page.get_by_role("heading", name="导入业务资料", exact=True)
                ).first
                upload_panel.scroll_into_view_if_needed()
                page.wait_for_timeout(650)
                page.get_by_label("业务资料文件", exact=True).set_input_files(str(fixture))
                page.wait_for_timeout(1_050)
                with legacy.capture_responses(
                    page,
                    lambda response: response.request.method == "POST"
                    and legacy.api_path(response.url)
                    == f"/api/v1/businesses/{business_id}/artifacts",
                ) as upload_captures:
                    slow_click(
                        page,
                        upload_panel.get_by_role("button", name="上传并建立索引", exact=True),
                        before_ms=1_000,
                        after_ms=0,
                    )
                    upload_response = legacy.wait_capture(
                        page,
                        upload_captures,
                        lambda item: item["path"].endswith("/artifacts"),
                        description="上传资料",
                    )
                if upload_response["status"] != 202:
                    raise RuntimeError(
                        f"上传资料失败：HTTP {upload_response['status']} "
                        f"{upload_response.get('text', upload_response.get('json'))}"
                    )

                if "json" in upload_response:
                    upload = legacy.captured_json(upload_response, description="上传资料")
                    legacy.wait_for_job(client, upload["job_id"], args.timeout)
                else:
                    upload = {
                        "job_id": legacy.current_session_job_id(page),
                        "artifact_id": None,
                        "status": "queued",
                    }
                    if upload["job_id"]:
                        legacy.wait_for_job(client, upload["job_id"], args.timeout)
                    else:
                        legacy.wait_for_artifact_completion(
                            client, business_id, args.timeout
                        )
                expect(page.get_by_text("后台任务：completed", exact=False)).to_be_visible()
                page.wait_for_timeout(1_400)
                profile = legacy.get_json(client, f"/businesses/{business_id}")
                if profile.get("coverage", {}).get("files") != 5:
                    raise RuntimeError(f"静态解析文件数异常：{profile.get('coverage')}")
                screenshots.append(
                    legacy.shot(page, screenshot_dir, "showcase-01-onboarding-parsed")
                )
                steps.append(
                    {
                        "name": "static-parse",
                        "status": "completed",
                        "job_id": upload["job_id"],
                        "files": profile["coverage"]["files"],
                        "profile_version": profile["profile_version"],
                    }
                )

                page.get_by_role("heading", name="资料来源与处理状态", exact=True).scroll_into_view_if_needed()
                page.wait_for_timeout(3_200)
                screenshots.append(
                    legacy.shot(page, screenshot_dir, "story-01-static-parsing")
                )
                page.wait_for_timeout(1_200)

                slow_click(
                    page,
                    page.get_by_role("button", name="业务概览", exact=False).first,
                    before_ms=850,
                    after_ms=1_300,
                )
                page.get_by_role("heading", name="业务概览", exact=True).wait_for()
                expect(page.get_by_label("选择业务", exact=True)).to_have_value(business_id)
                page.wait_for_timeout(3_200)
                screenshots.append(
                    legacy.shot(page, screenshot_dir, "story-02-business-profile")
                )

                services_heading = page.get_by_role(
                    "heading", name="服务与角色", exact=True
                ).first
                services_heading.scroll_into_view_if_needed()
                page.wait_for_timeout(2_800)
                dependency_graph = page.get_by_role(
                    "img", name="业务服务和模块依赖图", exact=True
                ).first
                dependency_graph.scroll_into_view_if_needed()
                page.wait_for_timeout(4_200)
                screenshots.append(
                    legacy.shot(page, screenshot_dir, "story-03-profile-graph")
                )

                open_structure_page(page, screenshot_dir, system_id)
                expect(
                    page.get_by_role(
                        "heading", name="业务结构图", exact=True
                    )
                ).to_be_visible(timeout=30_000)
                diagram_frame = page.locator(
                    'iframe[title="可交互的业务结构与流程图"]'
                ).first
                diagram_frame.wait_for(timeout=45_000)
                diagram = diagram_frame.content_frame
                diagram_width = 0.0
                for _ in range(2):
                    diagram.locator("[data-node-id]").first.wait_for(timeout=45_000)
                    page.wait_for_timeout(1_500)
                    iframe_handle = diagram_frame.element_handle()
                    diagram_page = (
                        iframe_handle.content_frame() if iframe_handle else None
                    )
                    if diagram_page is None:
                        raise RuntimeError("业务结构图 iframe 尚未关联文档")
                    diagram_width = float(
                        diagram_page.evaluate(
                            """() => document.querySelector(".container")
                                ?.getBoundingClientRect().width || innerWidth"""
                        )
                    )
                    if diagram_width >= 800:
                        break
                    diagram_frame.evaluate("element => { element.srcdoc = element.srcdoc; }")
                    page.wait_for_timeout(1_500)
                    diagram = diagram_frame.content_frame
                if diagram_width < 800:
                    raise RuntimeError(
                        f"业务结构图内嵌宽度异常：{diagram_width:.1f}px"
                    )
                expect(
                    page.get_by_text("资料版本 v", exact=False)
                ).to_be_visible(timeout=45_000)
                page.wait_for_timeout(5_000)
                screenshots.append(
                    legacy.shot(page, screenshot_dir, "story-04-structure-graph")
                )

                code_node = (
                    diagram.locator("[data-node-id]")
                    .filter(has_text="src.api")
                    .first
                )
                code_node.click(force=True)
                page.wait_for_timeout(2_000)
                source_panel = page.locator(".source-panel")
                source_panel.scroll_into_view_if_needed()
                expect(source_panel).to_contain_text("Static", timeout=20_000)
                expect(source_panel).to_contain_text("src/api.py", timeout=20_000)
                page.wait_for_timeout(3_600)
                source_button = source_panel.get_by_role(
                    "button", name="查看该版本原件", exact=True
                ).first
                source_button.click()
                page.wait_for_timeout(2_200)
                code_preview = page.locator("section.structure-panel > pre").first
                expect(code_preview).to_be_visible(timeout=20_000)
                expect(
                    page.locator('section.structure-panel [role="alert"]')
                ).not_to_be_visible()
                source_panel.evaluate(
                    "element => element.scrollIntoView({block: 'center'})"
                )
                page.wait_for_timeout(3_200)
                screenshots.append(
                    legacy.shot(page, screenshot_dir, "story-05-source-trace")
                )
                steps.append(
                    {
                        "name": "business-graph-understood",
                        "status": "completed",
                        "business_id": business_id,
                        "profile_version": profile["profile_version"],
                        "services": len(profile.get("services", [])),
                        "dependencies": len(profile.get("dependencies", [])),
                        "source_references": len(profile.get("source_references", [])),
                        "diagram_width": round(diagram_width, 1),
                    }
                )

                set_caption(page, 2, marks, started_monotonic)
                slow_click(
                    page,
                    page.get_by_role("button", name="业务概览", exact=False).first,
                    before_ms=650,
                    after_ms=950,
                )
                page.get_by_role("heading", name="业务概览", exact=True).wait_for()
                page.wait_for_timeout(900)

                for service, desired in (("catalog-api", "application"), ("store", "datastore")):
                    role_select = page.get_by_label(f"{service}角色", exact=True)
                    if role_select.input_value() != desired:
                        slow_select(page, role_select, desired)
                    else:
                        role_select.scroll_into_view_if_needed()
                        page.wait_for_timeout(750)

                dependency_row = (
                    page.locator(".business-row")
                    .filter(has_text="service:catalog-api")
                    .filter(has_text="depends_on")
                    .filter(has_text="service:store")
                    .first
                )
                dependency_row.scroll_into_view_if_needed()
                page.wait_for_timeout(1_000)
                if "已确认" not in dependency_row.inner_text():
                    with legacy.capture_responses(
                        page,
                        lambda response: legacy.response_is(
                            response,
                            "POST",
                            f"/api/v1/businesses/{business_id}/review",
                        ),
                    ) as relation_captures:
                        slow_click(
                            page,
                            dependency_row.get_by_role("button", name="确认", exact=True),
                            before_ms=1_100,
                            after_ms=0,
                        )
                        relation_response = legacy.wait_capture(
                            page,
                            relation_captures,
                            lambda item: item["path"].endswith("/review"),
                            description="确认依赖关系",
                        )
                    if relation_response["status"] != 200:
                        raise RuntimeError(
                            "确认依赖关系失败："
                            f"HTTP {relation_response['status']} "
                            f"{relation_response.get('text', relation_response.get('json'))}"
                        )
                    page.wait_for_timeout(1_250)
                expect(
                    page.locator(".business-row")
                    .filter(has_text="service:catalog-api")
                    .filter(has_text="depends_on")
                    .filter(has_text="已确认")
                    .first
                ).to_be_visible()

                confirm_profile = page.get_by_role("button", name="确认档案", exact=True).first
                with legacy.capture_responses(
                    page,
                    lambda response: legacy.response_is(
                        response, "POST", f"/api/v1/businesses/{business_id}/review"
                    ),
                ) as profile_captures:
                    slow_click(page, confirm_profile, before_ms=1_100, after_ms=0)
                    profile_response = legacy.wait_capture(
                        page,
                        profile_captures,
                        lambda item: item["path"].endswith("/review"),
                        description="确认业务档案",
                    )
                if profile_response["status"] != 200:
                    raise RuntimeError(
                        f"确认业务档案失败：HTTP {profile_response['status']} "
                        f"{profile_response.get('text', profile_response.get('json'))}"
                    )
                page.wait_for_timeout(1_300)
                profile = legacy.get_json(client, f"/businesses/{business_id}")
                if profile.get("review_status") != "approved":
                    raise RuntimeError(f"业务档案未获批准：{profile.get('review_status')}")
                screenshots.append(
                    legacy.shot(page, screenshot_dir, "showcase-02-business-reviewed")
                )
                steps.append(
                    {
                        "name": "business-reviewed",
                        "status": "completed",
                        "profile_version": profile["profile_version"],
                    }
                )

                slow_click(
                    page,
                    page.get_by_role("button", name="服务器与服务", exact=False).first,
                    before_ms=700,
                    after_ms=1_000,
                )
                page.get_by_role("heading", name="服务器与服务", exact=True).wait_for()
                page.wait_for_timeout(1_000)

                target_panel = page.locator("section.panel").filter(
                    has=page.get_by_role("heading", name="关联 Linux 环境", exact=True)
                ).first
                slow_fill(
                    page,
                    target_panel.get_by_label("环境名称", exact=True),
                    "Hackathon Isolated Mock",
                    delay_ms=14,
                )
                connector_select = target_panel.locator('select:has(option[value="mock"])')
                expect(connector_select).to_have_value("mock")
                slow_select(
                    page,
                    target_panel.locator('select[name="scenario"]'),
                    "mixed",
                )
                slow_fill(
                    page,
                    target_panel.locator('textarea[name="paths"]'),
                    "/srv/catalog",
                    delay_ms=12,
                )
                with legacy.capture_responses(
                    page,
                    lambda response: response.request.method == "POST"
                    and legacy.api_path(response.url)
                    == f"/api/v1/businesses/{business_id}/targets",
                ) as target_captures:
                    slow_click(
                        page,
                        target_panel.get_by_role("button", name="保存环境关联", exact=True),
                        before_ms=800,
                        after_ms=0,
                    )
                    target_response = legacy.wait_capture(
                        page,
                        target_captures,
                        lambda item: item["path"].endswith("/targets"),
                        description="创建 Mock 目标",
                    )
                if target_response["status"] != 201:
                    raise RuntimeError(
                        f"创建 Mock 目标失败：HTTP {target_response['status']} "
                        f"{target_response.get('text', target_response.get('json'))}"
                    )
                target = legacy.captured_json(target_response, description="创建 Mock 目标")
                target_id = target["id"]
                marks["target_id"] = target_id
                expect(page.get_by_label("运行环境", exact=True)).to_have_value(target_id)
                page.wait_for_timeout(1_300)

                discovery_panel = page.locator("section.panel").filter(
                    has=page.get_by_role("heading", name="轻量发现与范围审核", exact=True)
                ).first
                with legacy.capture_responses(
                    page,
                    lambda response: response.request.method == "POST"
                    and legacy.api_path(response.url)
                    == f"/api/v1/businesses/{business_id}/targets/{target_id}/discover",
                ) as discovery_captures:
                    slow_click(
                        page,
                        discovery_panel.get_by_role("button", name="定向轻量发现", exact=True),
                        before_ms=800,
                        after_ms=0,
                    )
                    discovery_response = legacy.wait_capture(
                        page,
                        discovery_captures,
                        lambda item: item["path"].endswith("/discover"),
                        description="启动范围发现",
                    )
                if discovery_response["status"] != 202:
                    raise RuntimeError(
                        f"启动范围发现失败：HTTP {discovery_response['status']} "
                        f"{discovery_response.get('text', discovery_response.get('json'))}"
                    )
                discovery_start = legacy.captured_json(
                    discovery_response, description="启动范围发现"
                )
                legacy.wait_for_job(
                    client, discovery_start["job_id"], args.timeout
                )
                page.wait_for_timeout(1_400)

                review_binding(
                    page,
                    service="catalog-api",
                    kind="service",
                    decision="confirm",
                    description="确认 catalog-api 服务范围",
                )
                review_binding(
                    page,
                    service="store",
                    kind="container",
                    decision="confirm",
                    description="确认 store 容器范围",
                )

                bindings = legacy.list_value(
                    legacy.get_json(client, f"/businesses/{business_id}/bindings")
                )
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
                store_binding = next(
                    (
                        item
                        for item in bindings
                        if item.get("target_id") == target_id
                        and item.get("service") == "store"
                        and item.get("kind") == "container"
                        and item.get("authorization_valid")
                    ),
                    None,
                )
                if not catalog_binding or not store_binding:
                    raise RuntimeError("未同时找到 catalog-api 服务与 store 容器的有效持久授权")
                page.wait_for_timeout(2_500)
                screenshots.append(
                    legacy.shot(page, screenshot_dir, "story-06-scope-confirmed")
                )
                steps.append(
                    {
                        "name": "scope-authorized",
                        "status": "completed",
                        "catalog_binding": catalog_binding["id"],
                        "store_binding": store_binding["id"],
                    }
                )

                set_caption(page, 3, marks, started_monotonic)
                slow_click(
                    page,
                    page.get_by_role("button", name="排查故障", exact=False).first,
                    before_ms=800,
                    after_ms=1_100,
                )
                page.get_by_role("heading", name="排查故障", exact=True).wait_for()
                page.wait_for_timeout(1_000)

                system_select = page.locator('select[name="system_id"]')
                expect(system_select).to_have_value(system_id)
                expect(page.get_by_label("本次调查 SSH 目标", exact=True)).to_have_value(target_id)
                slow_fill(
                    page,
                    page.locator('textarea[name="symptom"]'),
                    args.symptom,
                    delay_ms=36,
                )
                task_select = page.locator('select[name="task_type"]')
                expect(task_select).to_have_value("focused_diagnosis")
                explicit = task_select.evaluate(
                    "element => element.dataset.explicit === 'true'"
                )
                if explicit:
                    raise RuntimeError("任务类型被标记为显式选择，无法验证症状文本路由")
                page.wait_for_timeout(1_300)
                screenshots.append(
                    legacy.shot(page, screenshot_dir, "story-07-investigation-ready")
                )

                if args.stop_before_run:
                    steps.append(
                        {
                            "name": "dry-run-stop",
                            "status": "completed",
                            "detail": "Stopped before incident and Run creation.",
                        }
                    )
                    show_end_card(page, dry_run=True, run=None, usage=None)
                    add_mark(marks, started_monotonic, "card", "end_card_started")
                    page.wait_for_timeout(4_000)
                else:
                    with legacy.capture_responses(
                        page,
                        lambda response: response.request.method == "POST"
                        and (
                            legacy.api_path(response.url) == "/api/v1/incidents"
                            or legacy.api_path(response.url).endswith("/runs")
                        ),
                    ) as run_captures:
                        slow_click(
                            page,
                            page.get_by_role(
                                "button", name="提交故障并开始调查", exact=False
                            ).first,
                            before_ms=1_200,
                            after_ms=700,
                        )
                        incident_response = legacy.wait_capture(
                            page,
                            run_captures,
                            lambda item: item["path"] == "/api/v1/incidents",
                            description="创建故障事件",
                        )
                        run_response = legacy.wait_capture(
                            page,
                            run_captures,
                            lambda item: item["path"].endswith("/runs"),
                            description="启动故障调查",
                        )
                    if incident_response["status"] not in {200, 201}:
                        raise RuntimeError(
                            f"创建事件失败：HTTP {incident_response['status']} "
                            f"{incident_response.get('text', incident_response.get('json'))}"
                        )
                    if run_response["status"] != 202:
                        raise RuntimeError(
                            f"启动调查失败：HTTP {run_response['status']} "
                            f"{run_response.get('text', run_response.get('json'))}"
                        )
                    incident_request = incident_response.get("request_json") or {}
                    if "task_type" in incident_request:
                        raise RuntimeError(
                            f"意外显式提交了 task_type：{incident_request.get('task_type')}"
                        )

                    try:
                        incident_id = legacy.captured_json(
                            incident_response, description="创建故障事件"
                        )["id"]
                    except legacy.DemoFlowError:
                        pass
                    try:
                        run_id = legacy.captured_json(
                            run_response, description="启动故障调查"
                        )["run_id"]
                    except legacy.DemoFlowError:
                        pass
                    if not incident_id or not run_id:
                        incident = legacy.wait_for_incident_run(
                            client, system_id, args.timeout
                        )
                        incident_id = incident_id or incident["id"]
                        run_id = run_id or incident["latest_run_id"]
                    marks["incident_id"] = incident_id
                    marks["run_id"] = run_id
                    page.locator(".run-view").wait_for()
                    page.wait_for_timeout(1_100)
                    final_run = legacy.wait_for_run(client, run_id, args.timeout)
                    expect(page.locator(".run-view")).to_contain_text(
                        "执行状态：已结束", timeout=args.timeout * 1000
                    )

                    diagnosis = final_run.get("diagnosis_status") or (
                        final_run.get("diagnosis_result") or {}
                    ).get("diagnosis_status")
                    final_usage = legacy.run_usage(final_run)
                    if diagnosis != "direct_cause_supported":
                        raise RuntimeError(
                            f"Story expected direct_cause_supported, got {diagnosis}"
                        )
                    if final_run.get("completion_satisfied") is not True:
                        raise RuntimeError("completion_satisfied is not true")
                    if final_usage.get("model_calls") != 0:
                        raise RuntimeError(
                            f"Story expected 0 model calls, got {final_usage.get('model_calls')}"
                        )
                    if final_usage.get("tool_calls") != 3:
                        raise RuntimeError(
                            f"Story expected 3 tool calls, got {final_usage.get('tool_calls')}"
                        )

                    conclusion = page.get_by_test_id("investigation-conclusion")
                    expect(conclusion).to_be_visible(timeout=30_000)
                    conclusion.scroll_into_view_if_needed()
                    page.wait_for_timeout(2_400)

                    audit = page.get_by_test_id("evidence-audit")
                    audit.scroll_into_view_if_needed()
                    if not audit.get_attribute("open"):
                        slow_click(
                            page,
                            audit.locator("summary").first,
                            before_ms=600,
                            after_ms=900,
                        )

                    tool_summary = audit.get_by_text(
                        "每次实际执行的工具与策略结果", exact=True
                    ).first
                    tool_summary.scroll_into_view_if_needed()
                    tool_details = tool_summary.locator("xpath=..")
                    if not tool_details.get_attribute("open"):
                        slow_click(
                            page,
                            tool_summary,
                            before_ms=450,
                            after_ms=700,
                        )
                    expect(
                        audit.get_by_text("service.list", exact=True).first
                    ).to_be_visible(timeout=20_000)
                    page.wait_for_timeout(3_400)

                    show_investigation_ledger(page, final_run)
                    add_mark(
                        marks,
                        started_monotonic,
                        "card",
                        "action_ledger_started",
                    )
                    page.wait_for_timeout(11_500)
                    screenshots.append(
                        legacy.shot(
                            page,
                            screenshot_dir,
                            "story-08-action-ledger",
                        )
                    )
                    remove_overlays(page)

                    timeline_heading = audit.get_by_text(
                        "审计时间线", exact=True
                    ).first
                    timeline_heading.scroll_into_view_if_needed()
                    page.wait_for_timeout(5_500)
                    screenshots.append(
                        legacy.shot(
                            page,
                            screenshot_dir,
                            "story-09-evidence-timeline",
                        )
                    )

                    set_caption(page, 4, marks, started_monotonic)
                    conclusion.scroll_into_view_if_needed()
                    expect(page.get_by_test_id("ab-fact-report")).to_be_visible(
                        timeout=30_000
                    )
                    page.wait_for_timeout(5_200)

                    fact_report = page.get_by_test_id("ab-fact-report")
                    fact_report.scroll_into_view_if_needed()
                    page.wait_for_timeout(6_200)
                    screenshots.append(
                        legacy.shot(
                            page,
                            screenshot_dir,
                            "story-10-evidence-gate",
                        )
                    )

                    steps.append(
                        {
                            "name": "investigation-completed",
                            "status": "completed",
                            "run_id": run_id,
                            "diagnosis": diagnosis,
                            "usage": final_usage,
                        }
                    )

                    set_caption(page, 5, marks, started_monotonic)
                    page.wait_for_timeout(700)
                    show_result_card(page, final_run, final_usage)
                    add_mark(
                        marks,
                        started_monotonic,
                        "card",
                        "result_card_started",
                    )
                    page.wait_for_timeout(8_600)
                    screenshots.append(
                        legacy.shot(
                            page,
                            screenshot_dir,
                            "story-11-result-card",
                        )
                    )
                    remove_overlays(page)

                    set_caption(page, 6, marks, started_monotonic)
                    slow_click(
                        page,
                        page.get_by_role(
                            "button", name="排障经验", exact=False
                        ).first,
                        before_ms=550,
                        after_ms=1_000,
                    )
                    page.get_by_role(
                        "heading", name="排障经验", exact=True
                    ).wait_for()
                    page.wait_for_timeout(3_800)
                    show_learning_card(page)
                    add_mark(
                        marks,
                        started_monotonic,
                        "card",
                        "learning_card_started",
                    )
                    page.wait_for_timeout(10_800)
                    screenshots.append(
                        legacy.shot(
                            page,
                            screenshot_dir,
                            "story-12-learning-loop",
                        )
                    )
                    remove_overlays(page)

                    show_end_card(
                        page,
                        dry_run=False,
                        run=final_run,
                        usage=final_usage,
                    )
                    add_mark(marks, started_monotonic, "card", "end_card_started")
                    page.wait_for_timeout(9_000)

                add_mark(marks, started_monotonic, "recording", "capture_finished")
                marks["recorded_seconds"] = round(time.monotonic() - started_monotonic, 3)
                if page_errors:
                    steps.append(
                        {
                            "name": "browser-errors",
                            "status": "warning",
                            "details": page_errors,
                        }
                    )
                if http_errors:
                    steps.append(
                        {
                            "name": "http-errors",
                            "status": "warning",
                            "details": http_errors,
                        }
                    )
            except Exception:
                flow_error = traceback.format_exc()
            finally:
                try:
                    context.close()
                finally:
                    browser.close()
            if video is not None:
                try:
                    video_source = Path(video.path())
                except Exception:
                    if flow_error:
                        flow_error += "\n" + traceback.format_exc()
                    else:
                        flow_error = traceback.format_exc()

    if video_source and video_source.is_file():
        capture_path.unlink(missing_ok=True)
        shutil.move(str(video_source), str(capture_path))

    if not args.stop_before_run and final_run:
        run_json_path.write_text(
            json.dumps(final_run, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    caption_times = {
        item.get("name"): float(item.get("elapsed_seconds", 0.0))
        for item in marks["captions"]
    }
    marks["narration_anchors"] = [
        {
            "index": index + 1,
            "text": text,
            "start_seconds": round(
                caption_times.get(f"caption_{index + 1}", index * 18.0) + 0.9,
                3,
            ),
        }
        for index, text in enumerate(NARRATION_LINES)
    ]
    narration_script_path.write_text(
        json.dumps(
            {
                "voice": "Microsoft Huihui Desktop (zh-CN)",
                "rate": -2,
                "lines": NARRATION_LINES,
                "caption_marks": marks["captions"],
                "narration_anchors": marks["narration_anchors"],
                "end_card_seconds": next(
                    (
                        item["elapsed_seconds"]
                        for item in marks["events"]
                        if item["name"] == "end_card_started"
                    ),
                    None,
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    marks["steps"] = steps
    marks["status"] = "completed" if flow_error is None else "failed"
    marks_path.write_text(
        json.dumps(marks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_showcase_report(
        report_path,
        started_at=started_at,
        finished_at=now_iso(),
        dry_run=args.stop_before_run,
        error=flow_error,
        marks=marks,
        video_path=capture_path if capture_path.is_file() else None,
        screenshots=screenshots,
        run=final_run,
        capture_name=args.capture_name,
    )

    print(f"Capture: {capture_path if capture_path.is_file() else 'not generated'}")
    print(f"Marks: {marks_path}")
    print(f"Report: {report_path}")
    if flow_error:
        print(flow_error, file=sys.stderr)
        return 1
    if final_run:
        print(f"diagnosis_status={final_run.get('diagnosis_status')}")
        print(f"run_id={final_run.get('id')}")
    else:
        print("Dry run completed; no investigation Run created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
