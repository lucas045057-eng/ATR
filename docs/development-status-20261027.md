# ATR 开发情况汇总（2026-10-27）

定位：本地单操作员 SRE 诊断运行时。主线：上传资料 → 有界静态解析+业务图 → 人工审核 → Mock/Docker只读/SSH只读授权 → 有界只读调查（模型只能选 Runtime 提供的 `action_id`）→ 确定性 Evidence Gate 定结论等级。永不修复、不确认超出证据的根因。运行主线 **V2.3 + `ab_v1`**（README 已随 `5f0bc7c` 对齐）。

## 一、核心模块状态（全部已在仓库中，真实运行）

| 模块 | 状态 |
|---|---|
| 业务资料接入（ZIP/TAR/源码/配置/文档/图片PDF） | ✅ 已运行（演示 5 文件解析 4/5，web/app.js 标记 parser_unavailable 如实显示） |
| 静态解析 + 业务图 + Archify 可视化 | ✅（7 节点/5 边/4 依赖/5 来源引用） |
| 业务画像 + 人工审核 + 版本 | ✅（v2→v4 approved） |
| Mock / SSH / standard_ssh / Docker只读 连接器与授权边界 | ✅（Mock 双绑定确认、无关对象未授权） |
| ab_v1 有界调查 Runtime（`ab_investigation.py` 899行） | ✅（focus 判别/自动收敛/任务停止策略全部可用） |
| Evidence Gate / 事实报告 / 反证 | ✅（演示 Run5 苏州 direct_cause_supported;依赖链缺 endpoint mapping 时会停在 partial，不美化） |
| 决算预算 / 脱敏 / 审计 / 权限白名单 | ✅ |
| 知识循环（Candidate/Trusted Pattern、修复核验） | ✅ 框架在（本次演示知识页如实的空，未伪造） |
| 外部模型（`EXTERNAL_MODEL_ENABLED=false` 默认） | ✅ 本地可选启用 + 有离线兜底路径 |

## 二、比赛交付（已完成）

- `output/atr-hackathon-demo-final.mp4`：185s `Understand→Investigate→Verify→Learn` 展播版（1600×900，H.264+AAC，中文字幕+Huihui 人声旁白，已由你确认）
- 静音版、44s/68s/只读版备用视频
- `output/atr-source-20261023.zip`：476 文件源码包，含演示与录制脚本，SHA-256 `CCC9...C3832` 校验通过
- 验证报告：`output/demo-verification.md`（累计 32.8 KB）、`output/story-verification.md`、`output/story-marks.json`
- 关键叙事数字（真实）：`direct_cause_supported` / 3 次只读工具 / 0 模型调用 / 无关对象保持未授权

## 三、代码与工作树现状

- 分支 `codex/ab-v1.1`，HEAD `5f0bc7c`（README 刷新）；未提交改动：
  - `backend/app/business/source_index.py`：模型上下文补 `content_digest` 字段（待定是否提交）
  - 8 个测试文件删改（~163 行，对应历史行为移除）：同上待定
  - `scripts/run-local.ps1`：UTF-8 BOM 修复（PS 5.1 不再解析报错）
- 未跟踪可用工具：`scripts/demo_record_{story,showcase,final}.py`、`scripts/mux_final_video.py`、`output/build_source_package.py`、`output/gen_narration.ps1` 等
- 本地 API 未运行（需要时 8010 起：`python -m uv run uvicorn app.main:app --app-dir backend --port 8010 --workers 1`；8000 被 Orbital 占用）

## 四、已知风险与未完成项

1. **全量 pytest 未在本机恢复**：裁剪报告记录的是"裁剪前 1093 passed / 10 failed（预算、模型兼容、映射边界、SSH）"，这 10 个失败是历史遗留，不是新回归；本机尚未跑出新的完整基线数字。
2. **模型全路径未真实压测**：比赛演示路径刻意保持 0 模型调用收敛；SAPI/Huihui 之外的模型调试、SSE 流式、多轮上下文等未做端到端验证。
3. **output/ 缓存未清理**：TTS 探针 wav/ps1/obj/exe、帧截图等一次性产物还留在这里，提交前应清理。
4. **知识库叙事边界**：演示 Run 未产生 Candidate Pattern（无修复核验），Learn 段只展示了机制展示，不能宣称"已沉淀经验"。

## 五、建议后续工作（优先级）

1. 恢复绿基线：`uv sync --locked` → `pytest -q` → `ruff check .` → `npm ci && npm run build`，甄别并登记 10 个历史失败。
2. 决定未提交三组改动的去留（`source_index.py` content_digest、8 个测试文件、run-local.ps1 BOM）。
3. 清理 output/ 缓存，保留交付件 + marks + 验证 md。
