# ATR — Adaptive Troubleshooting Runtime

ATR 是一个本地单操作员 SRE 故障调查运行时：先理解陌生业务，再在明确授权的范围内收集只读证据，由 Evidence Gate 决定结论等级。

```text
业务资料 → 有界静态解析 → 可溯源业务图 → 人工审核
       → Mock / Docker 只读 / SSH 只读目标
       → 受限调查动作 → 当前证据 → Evidence Gate
       → 人工修复与恢复验证 → Candidate / Trusted Pattern
```

当前运行主线是 **V2.3 + `ab_v1`**。Python/npm 包元数据仍为 `2.2.0`；本文按当前代码和工作台行为描述。

ATR 不执行自动修复，也不把模型的自报置信度当作根因证据。模型只能从 Runtime 提供的已授权 `action_id` 中选择动作；工具目标、参数、权限和证据关系由 Runtime 与确定性策略控制。

## 当前能力

| 能力 | 当前状态 |
|---|---|
| 资料接入 | ZIP、TAR/TAR.GZ、源码、配置、文档和常见图片/PDF 入口；解包与读取有大小、深度、文件数和超时限制 |
| 静态理解 | 解析服务、角色、模块、接口、依赖、配置和来源，保存文件摘要与版本 |
| 业务图谱 | 图节点、依赖关系、来源、未知项和冲突；关键事实需要人工确认 |
| 源码按需查看 | 按路径、符号和关键词检索有限上下文，并返回版本、摘要和行号 |
| 运行范围 | Mock 隔离场景、Docker 只读采集、固定 SSH 只读工具；候选匹配只负责排序，确认才代表授权 |
| 故障调查 | `normal` / `graph_adaptive` 自适应调查，以及 `graph_only` / `graph_pattern` 对照模式 |
| Evidence Gate | 区分直接故障、根因、未解决、范围内未观察到故障和证据缺口 |
| 经验复用 | 记录修复事实、真实恢复验证和人工审核后，才可形成 Candidate 或 Trusted Pattern |
| 评估 | 支持成对 Graph / Graph + Pattern 记录，指标来自持久化 Run，不预置结果 |

当前工作台提交的新业务自适应调查使用 `ab_v1`。兼容读取旧 Run 的 `legacy_v22` 和 `minimal_v2` 快照；历史 Run 不会被重写。

## Windows 本地启动

需要 Python 3.11+。本地资料接入和 Mock 调查不需要 Docker 或外部模型；只有重建前端时才需要 Node.js/npm。

```powershell
python -m pip install uv
python -m uv sync --locked
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
New-Item -ItemType Directory -Force targets | Out-Null
```

编辑 `.env`，至少设置一个长度不少于 32 个字符的 `COLLECTOR_TOKEN`。默认配置会关闭外部模型和 SSH，适合先使用资料接入与本地 Mock 流程。

需要重建前端时：

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..
```

启动本地 API 和工作台：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-local.ps1
```

打开 <http://127.0.0.1:8000>。健康检查地址是 `/api/v1/health`。本地工作台是单操作员模式，不提供互联网发布、多租户或多人权限隔离。

## 使用流程

1. **新业务接入**：创建业务，上传源码、部署资料、配置或文档。解析只读取受限内容，不执行源码、不安装依赖、不运行 Compose。
2. **业务档案**：查看服务、角色、模块、接口、依赖、来源、未知项和冲突；逐条确认/否决关系，必要时修正角色，再确认档案版本。
3. **服务器范围**：创建并选择明确标注的 Mock、Docker 只读或 SSH 只读目标；执行轻量发现后，由操作员确认业务服务绑定。未确认对象不能进入深读。
4. **故障调查**：选择已确认的目标，创建故障并选择调查目标。`graph_adaptive` 需要已审核的业务档案和运行范围；没有有效映射时先发现、审核，再恢复同一 Run。
5. **结果与知识**：查看动作台账、事实、反证、证据时间线和 Gate 结果。人工修复后记录修复事实，执行允许的健康 GET 验证，经过知识审核后才可形成 Trusted Pattern；Agent 不实施修复。

调查页面展示实际工具目标、读取字节数、模型调用、Token 用量、停止原因和安全错误。未配置模型、证据不足、范围冲突或权限错误时，系统保持未解决并说明缺口。

## 模型、预算与协议

外部模型默认关闭。确认资料可以发送给供应商后，才设置 `MODEL_NAME`、`MODEL_API_KEY`、可选的 `MODEL_BASE_URL`，并将 `EXTERNAL_MODEL_ENABLED=true`。`MODEL_JSON_SUPPORTED=true` 是当前结构化决策的必要条件；视觉能力不会因为声明配置而自动启用。

常用配置项：

| 配置 | 默认/作用 |
|---|---|
| `EXTERNAL_MODEL_ENABLED` | `false`；是否允许外部模型调用 |
| `MODEL_JSON_SUPPORTED` | `true`；供应商是否支持结构化 JSON |
| `INVESTIGATION_INPUT_TOKENS` | `12000`；调查输入估算值 |
| `INVESTIGATION_MODEL_ROUNDS` | `8`；模型轮数上限配置 |
| `INVESTIGATION_TOOL_CALLS` | `20`；调查工具调用上限配置 |
| `SSH_ENABLED` | `false`；是否启用 SSH 目标 |
| `ALLOWED_HEALTH_URLS` | 允许执行恢复验证的精确 URL 列表 |

`ab_v1` 会记录模型调用、Token、读取量和费用估算；工作台的预算区显示为可观测账本，不应被理解为供应商计费上限或替代基础设施限流。调查仍受 Runtime 任务停止策略、固定工具调用边界、输出大小和连接超时约束。

## 安全边界

- 上传资料、日志、配置和模型输出都视为不可信输入；解包拒绝路径穿越、链接、设备、重复路径、加密成员和超限内容。
- 模型不能生成任意 Shell、注册工具、提交目标 ID、修改权限或直接确认事实。
- 服务、容器、配置路径和 SSH 目标必须在授权范围内；候选匹配、历史日志和模型置信度都不能替代当前授权与证据。
- SSH 使用固定主机公钥校验，禁用自动接收未知密钥、SSH agent、自由命令、PTY、SCP/SFTP 和 sudo。`forced_gateway` 需要服务器管理员配置的强制只读入口；`standard_ssh_readonly` 仍依赖服务器账号权限，客户端白名单不能把有写权限的账号变成只读。
- Docker Collector 只在显式 `docker-demo` profile 中启用。Docker socket 的 `:ro` 挂载不能消除 daemon 权限，只适用于可信本地隔离 Demo。
- Agent 永不重启服务、修改配置、删除文件、写数据库或执行修复。真实生产 Linux、真实外部模型供应商和完整生产根因准确率尚未验收。

## 本地 Demo

固定黑客松场景位于 `demo/hackathon-catalog`，完整步骤见 [Demo 手册](docs/demo-runbook.md) 和 [接入手册](docs/business-onboarding.md)。

运行 Demo 时应把 Mock 结果描述为隔离演示。`direct_cause_supported` 表示当前授权范围内的直接故障得到支持；它不等于完整根因已确认，也不代表真实生产环境验收。

## 测试与构建

```powershell
python -m uv sync --locked --group dev
Set-Location frontend
npm ci
npm run build
Set-Location ..
python -m uv run pytest -q --basetemp=test-results/local-new-run --junitxml=test-results/local.xml
python -m uv run ruff check .
```

浏览器流程需要 Playwright Chromium。完整测试会访问隔离夹具；跳过的测试不能视为通过。当前仓库仍有历史基线问题，运行全套检查时应区分既有失败与本次改动造成的回归。

## 文档索引

- [Runtime 架构](docs/ATR-V2.3-DECISION-RUNTIME-ARCHITECTURE.md)
- [任务停止策略](docs/ATR-V2.3-TASK-STOP-POLICY.md)
- [业务接入手册](docs/business-onboarding.md)
- [安全边界](docs/security-boundaries.md)
- [部署说明](docs/deployment.md)
- [预算与用量](docs/context-budget.md)
- [Demo 手册](docs/demo-runbook.md)
- [仓库精简报告](docs/repository-slimming-report.md)
