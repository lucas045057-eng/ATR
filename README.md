# Adaptive Troubleshooting Runtime · V2.2

ATR 在本地分析陌生业务资料，建立有来源的业务骨架，经过人工审核后识别服务器上的业务范围，再使用固定只读工具调查故障。源码、部署资料、运行观测和人工结论分别保存；历史 Pattern 只能建议检查，诊断仍须通过当前证据 Gate。

本版基于 main `aafd8183fa055f68138b432a788ec7831c9b9698` 增量开发，保留原架构、测试、数据和 Git 历史。新增有界发现与绑定时效、原子调查任务、多轮 Adaptive Planner、确定性预测/因果核验、范围约束的知识反馈和三模式评测。详细结果见 [V2.2 验收](docs/v2.2-acceptance-report.md)、[开发记录](docs/v2.2-development-report.md) 和 [限制](docs/v2.2-known-limitations.md)。

架构见 [V2.2 架构](docs/v2.2-architecture.md)，跨机器启动见 [部署说明](docs/deployment.md)。旧 V2.1 交接和验收文档作为历史记录保留。

## Windows 本地启动

需要 Python 3.11+；前端重新构建需要 Node.js/npm。交付 ZIP 已包含 `frontend/dist`，本地资料接入和 Mock 调查不需要 Docker 或模型 API。

```powershell
python -m pip install uv
python -m uv sync --locked
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
New-Item -ItemType Directory -Force targets | Out-Null
```

编辑 `.env`，将 `OPERATOR_TOKEN` 和 `COLLECTOR_TOKEN` 分别换为至少 32 字符的不同随机值。默认关闭外部模型和 SSH，保留此默认即可先完成资料导入。需要重建前端时：

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..
```

启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-local.ps1
```

打开 **http://127.0.0.1:8000**，在访问设置输入操作员令牌。API 健康检查为 `/api/v1/health`。只支持单 API 进程。WSL2、Docker 启动及数据恢复见 [部署说明](docs/deployment.md)。

## 使用流程

1. “新业务接入”：创建业务，上传 ZIP、TAR/TAR.GZ、源码、配置或文档；后台先做本地解析，不执行源码、不安装依赖。无资料也可以创建空档案。
2. “业务档案”：查看服务、模块、接口、依赖、未知项、来源和版本；逐条确认/否决关系、修正角色，再确认档案。图片/PDF 当前保留人工录入入口。
3. “服务器范围”：先选择明确标注的 Mock 隔离场景，轻量发现后确认业务服务候选。匹配分数仅排序。真实 SSH 须由服务器所有者配置受限账号和固定网关，提供已确认的主机公钥及凭据引用。
4. “故障调查”：创建对应系统的故障并开始调查；页面轮询后台任务，展示工具、数据量、证据、预算、假设与未解决原因。未配置模型时保持离线未解决。
5. 人工修复后记录修复事实，执行允许的健康 GET 验证，经过知识审核才能生成 Trusted Pattern；Agent 不实施修复。

业务入口 normal/graph_adaptive 使用动态规划；graph_only/graph_pattern 用于固定基线对照。旧 Collector Demo 支持固定调查，不能以 graph_adaptive 标签执行。超过授权范围只记录申请，无法验证时保持 unresolved。详见 [接入手册](docs/business-onboarding.md)、[V2.2 API 增量](docs/api-v2.2.md)、[安全边界](docs/security-boundaries.md)、[预算配置](docs/context-budget.md)。

## 模型与预算

在 `.env` 配置 `MODEL_NAME`、`MODEL_API_KEY`、可选 `MODEL_BASE_URL`，确认资料允许发送到该供应商后设置 `EXTERNAL_MODEL_ENABLED=true`。能力采用明确声明，未实现自动供应商能力探测。`MODEL_JSON_SUPPORTED` 须为 true；`MODEL_VISION_SUPPORTED` 不会启用尚未接入的图像适配器。

默认初始理解每请求输入 8,000、调查每请求输入 12,000、初始模型调用 6、调查模型轮数 8、调查工具 20。调用前保守预检，优先记录供应商实际 usage；估算和配置费率估算明确标识。可在业务页面提高已有初始预算后继续。

## 测试

```powershell
python -m uv sync --locked --group dev
Set-Location frontend
npm ci
npm run build
Set-Location ..
python -m uv run playwright install chromium
python -m uv run pytest -q --basetemp=test-results/local-new-run --junitxml=test-results/local.xml
python -m uv run ruff check .
```

本机 V2.2 最终 **180 passed / 0 failed / 0 skipped**，另行显式执行的实际隔离 Linux/OpenSSH **18 passed**。本机 Chrome 可通过 `$env:ATR_BROWSER_CHANNEL='chrome'` 使用；默认使用已安装的 Playwright Chromium。缺少前端产物的跳过不能计为通过；缺浏览器会失败。每次 Windows 测试使用新的 --basetemp，保留旧记录。

13 个隔离故障各两次、三模式共 78 次运行的注入/步骤/成本/真值见 [评测报告](docs/v2.2-evaluation-report.md)。模型为 Stub，Docker 观测经 Mock 重放；不能当作真实模型或生产准确率。CI 包含锁定依赖、Ruff、全量默认测试与前端构建，无真实凭据；未向远端发布或执行 Actions。

## 运行边界

- 默认无 Docker socket、无 SSH、无外部模型。本地单操作员令牌系统不提供多租户权限隔离。
- SSH 仅发送固定入口 `atr-read-v1` 和 JSON 参数，校验固定主机公钥。只读还须由服务器受限账号、强制命令及文件权限保证；客户端白名单不能保证账号没有写权限。
- 原 Docker Collector 只在显式 `docker-demo` profile 启动。它仍拥有 Docker daemon 权限，`:ro` socket 挂载不能消除此权限。仅用于可信本地隔离 Demo。
- Docker API/UI/Worker/SQLite 与原 A/B 故障恢复、Linux/OpenSSH 受限夹具已经实际隔离验收。真实模型供应商、生产 Linux 业务服务器、完整 Redis/Nginx 故障集及生产根因准确率未验证；当前 Gate 只认可五类保守机制。

[架构](docs/v2.2-architecture.md) · [审计与变更](docs/v2.2-development-report.md) · [验收](docs/v2.2-acceptance-report.md) · [限制与后续](docs/v2.2-known-limitations.md) · [原 Demo 手册](docs/demo-runbook.md)
