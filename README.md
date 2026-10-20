# Adaptive Troubleshooting Runtime · V2.2

ATR 在本地分析陌生业务资料，建立有来源的业务骨架，经过人工审核后识别服务器上的业务范围，再使用固定只读工具调查故障。源码、部署资料、运行观测和人工结论分别保存；历史 Pattern 只能建议检查，诊断仍须通过当前证据 Gate。

本版保留 ATR 的运行核心：资料接入、业务图审核、固定只读调查、证据 Gate、Runtime 停止策略和安全审计。历史验收结果与一次性实验记录已从仓库移除，避免把生成物当作产品代码维护。

当前 Runtime 设计见 [决策 Runtime 架构](docs/ATR-V2.3-DECISION-RUNTIME-ARCHITECTURE.md) 和 [任务停止策略](docs/ATR-V2.3-TASK-STOP-POLICY.md)，跨机器启动见 [部署说明](docs/deployment.md)。

当前增量包含独立模型 Worker 与安全错误诊断、SSH 内存密码会话和私钥认证、调查 Run 的显式目标绑定、无映射时的基础发现与审核恢复，以及静态图标的可撤销附件分类。仓库裁剪范围和保留内容见 [仓库精简报告](docs/repository-slimming-report.md)。

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
3. “服务器范围”：先选择明确标注的 Mock 隔离场景，轻量发现后确认业务服务候选。匹配分数仅排序。真实 SSH 支持私钥凭据引用或后端内存密码会话，均需已确认的主机公钥与明确授权。推荐服务器端强制网关；普通 SSH 模式明确标注客户端约束，仍使用固定只读工具与低权限账号检查。
4. “故障调查”：选择本次 SSH 目标，创建对应系统的故障并开始调查；多个目标必须明确选择，Run 和恢复过程保持原目标。无有效映射时先基础发现、人工审核，再恢复同一 Run 与预算。页面展示实际工具目标、证据、预算及安全错误；未配置模型时保持离线未解决。
5. 人工修复后记录修复事实，执行允许的健康 GET 验证，经过知识审核才能生成 Trusted Pattern；Agent 不实施修复。

业务入口 normal/graph_adaptive 使用动态规划；graph_only/graph_pattern 用于固定基线对照。旧 Collector Demo 支持固定调查，不能以 graph_adaptive 标签执行。超过授权范围只记录申请，无法验证时保持 unresolved。详见 [接入手册](docs/business-onboarding.md)、[安全边界](docs/security-boundaries.md)、[预算配置](docs/context-budget.md)。

## 模型与预算

在 `.env` 配置 `MODEL_NAME`、`MODEL_API_KEY`、可选 `MODEL_BASE_URL`，确认资料允许发送到该供应商后设置 `EXTERNAL_MODEL_ENABLED=true`。能力采用明确声明，未实现自动供应商能力探测。`MODEL_JSON_SUPPORTED` 须为 true；`MODEL_VISION_SUPPORTED` 不会启用尚未接入的图像适配器。

默认初始理解每请求输入 8,000、调查每请求输入 12,000、初始模型调用 6、调查模型轮数 8、调查工具 20。调用前保守预检，优先记录供应商实际 usage；估算和配置费率估算明确标识。可在业务页面提高已有初始预算后继续。

单次输入估算采用 UTF-8 字节数加消息开销，不等同于供应商分词或计费。`INVESTIGATION_INPUT_TOKENS` 控制新建调查的单次预检查；限额会保存到 Run 的独立预算，修改 `.env` 不会自动改写历史预算。Docker 部署修改配置后需重新创建 API 容器加载新环境。调查模型上下文去除重复假设历史和冗余动作字段，完整证据与审计仍保存在数据库；无法容纳时在发送前停止，显示限制类别及安全数值。

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

本地测试包含后端回归、模型隔离、Runtime 安全边界和浏览器流程；本机 Chrome/Edge 可通过 `$env:ATR_BROWSER_CHANNEL='chrome'` 或 `'msedge'` 使用。缺少前端产物或浏览器时测试会失败，不把跳过当作通过。

## 运行边界

- 默认无 Docker socket、无 SSH、无外部模型。本地单操作员令牌系统不提供多租户权限隔离。
- SSH 校验固定主机公钥，禁止 root、agent 和自动查找私钥。forced_gateway 仅发送 `atr-read-v1` 与 JSON；standard_ssh_readonly 使用固定命令，不接受模型生成的 Shell。客户端白名单不能保证账号没有写权限，服务器端强制只读边界需由所有者配置。
- 原 Docker Collector 只在显式 `docker-demo` profile 启动。它仍拥有 Docker daemon 权限，`:ro` socket 挂载不能消除此权限。仅用于可信本地隔离 Demo。
- Docker API/UI/Worker/SQLite 与原 A/B 故障恢复、Linux/OpenSSH 受限夹具已经实际隔离验收。真实模型供应商、生产 Linux 业务服务器、完整 Redis/Nginx 故障集及生产根因准确率未验证；当前 Gate 只认可五类保守机制。

[决策 Runtime 架构](docs/ATR-V2.3-DECISION-RUNTIME-ARCHITECTURE.md) · [任务停止策略](docs/ATR-V2.3-TASK-STOP-POLICY.md) · [部署](docs/deployment.md) · [安全边界](docs/security-boundaries.md) · [Demo 手册](docs/demo-runbook.md)
