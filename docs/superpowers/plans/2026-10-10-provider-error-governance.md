# 模型错误治理 Implementation Plan

> **For agentic workers:** 使用 superpowers:executing-plans，在当前会话依次实施并验证；用户已明确要求直接修复，不重复征求常规设计授权。

**Goal:** 529 等模型失败在调查、用量审计和中文界面中准确传递，安全收尾并保留证据。

**Architecture:** 继承独立 worker 与硬超时。给现有 model_usage_records 增加默认空的诊断 JSON；调查摘要传递同一安全诊断，不新增状态机或重试平台。

**Tech Stack:** 现有 Python 3.12 / FastAPI / LiteLLM 1.104.1 / SQLite / React。

**Spec:** 用户任务书 `C:\Users\Admin\.codex\attachments\b1d1827d-087c-4398-9283-b3cd8896b0ea\已粘贴的文本.txt`。

## Global Constraints

- 只操作 E:\ATR，保留现有未提交隔离修复和用户配置；不 push / merge / PR。
- num_retries=0，REAL_PAID_REQUESTS=0；不改 Base URL、模型名、现有 120 秒超时。
- Mock 范围之外不执行工具；不绕过 store 未授权绑定、证据门和预算。
- 更新部署容器必须在测试通过并确认无活动任务之后，保留 Volume。

## Review Focus

- 500+ 不可用与客户端连接错误的边界：HTTP 表驱动测试。
- 子进程无法启动、初始化卡住：明确阶段且释放预留。
- 非法 usage（负数、字符串、浮点、缺字段）：不能保存 actual。
- 第二轮供应商失败：不得覆写之前假设/反证，也不得新增离线 GPT 候选。
- Schema/引用错误带工具提议：校验失败前不能执行任何追加工具。

### Task 1: 分类与安全请求审计

Files: context/model_errors.py、model.py、model_worker.py、budget.py、storage/migrations.py；tests/test_provider_governance.py。
Interfaces: ModelCallError.diagnostic；ContextBudgetManager.settle(..., audit=None)；usage_records[].diagnostic。

- [x] 新增本地 HTTP 529、HTTP 分类、网络/进程失败、无效 usage 与审计测试，运行并确认 RED。
- [x] 依据状态码和确切异常类型分类；给旧表增加 diagnostics_json DEFAULT '{}'，改 INSERT 为显式列。
- [x] worker 传递初始化/请求计时；父进程补解析与总耗时；结算一次并留存关联 ID。
- [x] 运行定向测试，核对 GREEN 和旧预算行为。

### Task 2: 调查安全收尾与严格输出

Files: agent/workflow.py、新增 agent/model_schema.py、business_investigation.py、api/v1.py；调查集成测试。
Interfaces: workflow.model_error dict / report.analysis_status；summary.model_error、model_status、stage。

- [x] 写失败与历史保留、非法字段/引用、下一次恢复测试，确认 RED。
- [x] 输出校验在 usage 结算之前运行；失败不生成离线候选，不驱动工具，保留上一轮假设与反证。
- [x] unresolved + 明确 stop_reason 表示失败；后台 completed 只表示安全收尾完成。
- [x] 对业务和旧调查路径均验证；旧记录默认空诊断可读取。

### Task 3: UI、全套回归、Docker 与报告

Files: frontend/src/main.tsx、新增模型诊断组件；E2E 测试；docs/model-provider-error-governance-report.md。

- [x] 真浏览器 Fake 529 测试先确认缺少中文 HTTP 错误展示。
- [x] 显示模型、阶段、HTTP、原异常、重试次数和费用未知；说明人工重试可能收费。
- [x] 检查并调整实际轮询间隔/结束条件；保留轨迹可观测性。
- [x] 最终完整 pytest 验证完成；Ruff、前端构建、锁文件检查和隔离 Docker Mock 529/恢复已通过。
- [x] 备份和旧镜像已保存；最终测试结束、重新确认无任务后更新本地 API/Web，核对旧数据摘要与健康。
- [x] 只读复核闭合，中文报告草稿已生成；最终写入验证结果和指定状态字段。

## Execution ledger

Ruling: 用户明确要求只操作 E:\ATR 并继承工作区修复，因此复用 fix/litellm-runtime-isolation 分支，不创建外部 worktree。
Ruling: 用户要求自行推进常规实现，优先于技能的额外设计确认/提交步骤；不提交已有用户改动。
Pre-flight: Task 1 的 diagnostic 与 usage_records API 是 Task 2/3 的统一输入；保留原 code/status_code 字段并新增审计字段，旧记录使用空字典。
Audit finding: _planner 只保留异常类名，生成 offline report；调查未保存 model_error；700ms 轮询虽有停止条件但在 120 秒模型请求期间产生不必要流量。

Task 1/2: HTTP/worker/Schema/历史保留定向测试通过；先前全套233和补充242通过。最终全套重新运行，不使用前轮结果代替。
Review: 独立只读复核发现并关闭原始usage被SDK强转、追踪ID包含密钥、中断终态、初始化超时阶段、200后正文超时和异常usage丢失边界；已补精确测试。
UI validation: 真浏览器展示测试在实现后执行；初始529现状由任务书、日志和本地调查复现确认。终态轮询测试允许一次性最终刷新结束后计数。
Deployment: 独立Docker API无外网，合成密钥；529→人工新调查supported总共2次Mock请求，store读取0。正式Volume尚未替换。
Rollback: 原Web历史image层缺失；导出实际旧页面和Nginx配置重建恢复镜像。离线复制库的迁移回滚已验证41张业务表摘要一致。

Final verification:245 passed,0 failed,0 errors,0 skipped(357.98s)，Ruff/lock/build/diff checks通过；最后只读review无新Important/Critical。
Final deployment:重新确认活动任务0后保留Volume更新API/Web；APIhealthy、28000/28080健康200；41张业务表与8文件摘要全保留；Catalog v12 approved、3次旧调查API可读；.env/compose/config不变。
Completion:中文报告和状态字段写入；隔离Docker服务停止，临时数据和截图保留；真实付费请求0，无Git提交/push/merge/PR。
