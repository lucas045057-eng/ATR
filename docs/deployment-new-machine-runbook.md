# ATR 新机器部署与日常运维手册

**本文件是 ATR 唯一的部署事实源。** 原 `docs/deployment.md` 的 WSL2、Docker、备份恢复与升级内容已并入本手册并删除，历史版本见 git。

适用版本：运行主线 **V2.3 + `ab_v1`**（GitHub `main` 分支）。Python/npm 包元数据版本仍为 **2.2.0**，这是工程包版本号，不代表运行主线回退。

---

## 一、部署形态与端口

| 形态 | 启动方式 | 访问地址 |
|---|---|---|
| Windows 原生（本机已验证路径） | `.\scripts\atr.ps1 serve` | `http://127.0.0.1:8000` |
| WSL2 | `./scripts/atr serve` | `http://127.0.0.1:8000` |
| Docker | `docker compose up --build -d` | `http://127.0.0.1:8000` |

`scripts/run-local.ps1` / `run-local.sh` 仍保留，等价于转调 `.\scripts\atr.ps1 serve`（sh 下为 `./scripts/atr serve`）。

Docker 只有一个 API 容器：FastAPI 直接托管 `frontend/dist` 并提供 `/api`，不再有独立 nginx `web` 容器，也不再有 28000/28080/8080 等多套端口说法。需要换宿主机端口时设置 `ATR_HOST_PORT`（例如 `ATR_HOST_PORT=8001 docker compose up -d`）。Docker Collector 只在 `docker-demo` profile 下可选启动。

## 二、准备条件

| 项目 | 要求 | 说明 |
|---|---|---|
| Python | 3.11+（推荐 3.12） | 仅基础解释器；依赖由 uv 按 lock 安装 |
| Node.js / npm | 仅 git clone 后首次构建前端时需要 | 源码包/发布包已内置 `frontend/dist`，不需要 Node |
| 网络 | 首次 `uv sync` 需要访问 PyPI | 之后离线可运行资料接入与 Mock 流程 |
| 外部模型 | 默认不需要 | `EXTERNAL_MODEL_ENABLED=false` 即可跑通离线链路 |
| Docker | 可选 | 仅 Docker 部署方式需要 Linux Docker Engine |

## 三、获取代码

**方式 A — git clone**

```powershell
git clone <仓库地址> ATR
Set-Location ATR
git checkout main
```

需要固定版本时，请使用与当前主线一致的最新 tag。旧 tag `V1.0` 早于本次部署简化，其启动脚本仍要求 `.env`，**不适用本手册的零配置路径**。

`dist/` 在 `.gitignore` 中，clone 后没有构建产物，**首次启动前必须构建一次前端**（见第四节）。这是 clone 方式唯一需要 Node 的步骤。

**方式 B — 源码包 / 发布包（离线机器适用）**

使用开发机导出的 `output/atr-source-*.zip` 或 `output/atr-deploy-*.zip`，内置 `frontend/dist`，不含 `.env` 与密钥。解压即可，无需 git，也无需 Node。

## 四、安装与启动（Windows 原生）

```powershell
python -m pip install uv
python -m uv sync --locked
.\scripts\atr.ps1 init

# 仅 git clone 方式需要这一整段（含末尾的 Set-Location ..）；发布包/源码包已内置 dist，整段跳过
Set-Location frontend
npm ci
npm run build
Set-Location ..

.\scripts\atr.ps1 serve
```

验证 `uv sync --locked` 退出码为 0；clone 方式还要验证 `frontend/dist/index.html` 存在。若 npm 全局缓存无权限，可给 npm 配置项目内缓存目录后重装，无需管理员权限。

- `init`：`.env` 不存在时从 `.env.example` 生成，并把 `COLLECTOR_TOKEN` 替换为随机值；已存在则跳过、绝不覆盖。同时创建 `data/`、`data/artifacts/`、`targets/`。任一步失败会非 0 退出并回滚本次生成的文件。
- `serve`：固定单 worker 启动；默认 `127.0.0.1:8000`，可用 `--host` / `--port` 覆盖。启动前会打印访问地址与健康检查地址；`frontend/dist` 缺失时只警告并继续（API 可用，只是没有工作台）。

**`.env` 不是启动前提**：代码默认值已本地可跑，不执行 `init` 也能 `serve`；`init` 只是把随机 token 和目录一次备好（见第五节）。

可选命令：

```powershell
.\scripts\atr.ps1 doctor          # 体检 Python/dist/端口/数据目录/.env/token/roots/Docker
.\scripts\atr.ps1 doctor --json   # 结构化输出；任何 FAIL 时退出码为 1
.\scripts\atr.ps1 backup --out .\backups\2026-11-01
.\scripts\atr.ps1 version
```

旧入口 `powershell -File scripts/run-local.ps1` 仍可用，等价于 `atr.ps1 serve`。

验收清单（按顺序）：

1. `Invoke-RestMethod http://127.0.0.1:8000/api/v1/health` 正常，且返回 `version`、`build`、`decision_protocol`、`data_root`、`ui` 等诊断字段（`build` 来自 `ATR_BUILD` 环境变量，默认空；`ui=false` 表示 `frontend/dist` 缺失）。

   配置非法时（例如 `REPOSITORY_ROOTS` 指向不存在的目录），进程会在开始监听前退出，日志中打印「ATR 配置无效，无法启动：…」。这是有意保留的启动即失败；`/health` 的 `degraded` 只在运行期配置兜底时出现，不能用来诊断启动失败。
2. 浏览器打开 `http://127.0.0.1:8000`，工作台可加载。
3. 新建业务 → 上传一个小型源码/资料 ZIP → 解析生成业务图 → 审核档案。
4. 建 Mock 目标 → 发现 → 绑定 → 发起一次故障调查，确认动作台账与 Gate 结果正常产生（未配置模型时结论为诚实未解决即正常）。

**端口冲突处理**：若 8000 被占用，直接换端口启动（仍固定单 worker）：

```powershell
.\scripts\atr.ps1 serve --port 8001
```

## 五、配置 .env（可选，只写差异项）

默认值随代码提供，本地无需任何配置即可启动：

| 配置 | 默认 | 说明 |
|---|---|---|
| `DATABASE_PATH` | `./data/runtime.sqlite3` | SQLite 数据库，自动创建目录 |
| `ARTIFACT_ROOT` | `./data/artifacts` | 上传资料存储，自动创建目录 |
| `REPOSITORY_ROOTS` | `./demo` | 根目录必须存在；`demo/` 随仓库提供 |
| `COLLECTOR_URL` | `http://127.0.0.1:8090` | 仅 Docker Collector 场景使用 |
| `EXTERNAL_MODEL_ENABLED` | `false` | 外部模型默认关闭，离线可跑全流程（结论诚实降级） |
| `SSH_ENABLED` | `false` | 真实 SSH 目标默认关闭 |
| 限额类 | 见 `.env.example` | 上传/解包大小、文件数、超时、预算等 |

**关于 `COLLECTOR_TOKEN`**：只有一个凭据，且只在真正连接 Docker Collector 时才校验。跑资料接入 + Mock 调查不需要它。需要 `docker-demo` profile 时，必须设置为至少 32 字符的随机私密值，占位值（含 `replace-this`）会在**发起 Collector 请求前**被拒绝（构造客户端本身不报错，纯 HTTP 健康验证不受影响）；Collector 容器本身在 token 缺失、过短或为公开占位值（含 `replace-this`）时会直接启动失败，而不是静默返回 401。

生成随机值（仅供自己 `.env` 使用，不提交、不贴入聊天）：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**接入外部模型时**（确认资料可发给供应商后）：

```ini
MODEL_PROVIDER=openai-compatible
MODEL_BASE_URL=<供应商地址>
MODEL_API_KEY=<key>
MODEL_NAME=<模型名>
EXTERNAL_MODEL_ENABLED=true
MODEL_JSON_SUPPORTED=true    # 结构化决策为必要条件
```

**接入额外只读目标目录时**：先创建目录，再把路径追加到 `REPOSITORY_ROOTS`（例如 `REPOSITORY_ROOTS=./demo,./targets`）。根目录不存在时启动会明确报错。

## 六、日常运维操作

**启停**

- 启动：`.\scripts\atr.ps1 serve`（或 `serve --port 8001`）。
- 停止：Ctrl+C。重启保留数据库与资料，无需重装依赖。

**数据备份（建议每周或每次重要调查后）**

```powershell
.\scripts\atr.ps1 backup --out .\backups\2026-11-01
```

备份会生成一致性副本（`runtime.sqlite3`、`artifacts.tar`、`manifest.json`），目标目录必须不存在；有排队/运行中/等待确认的任务或进行中的调查时会拒绝并提示先结束。`backup` 按 `.env` 的 `DATABASE_PATH` 与 `ARTIFACT_ROOT` 取数（`--data` 仅用于旧版 `data/runtime.sqlite3` + `data/artifacts` 布局）；配置无法解析时会直接失败且不创建备份目录。没有 git 元数据时，`manifest.json` 的 `application_sha` 记录包版本并标记 `application_sha_kind=version`。也可手工停止 ATR 进程后备份 `./data/`（数据库 + artifacts）与 `.env` 本地副本。
- 不能在运行中直接复制 `runtime.sqlite3`（会漏 WAL）；要么停机复制，要么用 `sqlite3` backup 生成一致副本。
- 数据库包含业务图、Run 历史、Pattern；artifacts 包含上传资料原始文件。
- Docker 部署的数据在 `runtime-data` volume，恢复须由操作者停止容器后按自己的 volume 备份方案完成。

**安全运营红线**

- 不把 8000 端口暴露到互联网/局域网共享；工作台是单操作员模式，无多人权限隔离。
- `.env`、数据库、上传资料均属敏感数据，不提交 git、不随意外发。
- 真实 SSH 目标接入前必须先经服务器所有者授权并配置服务器侧只读强制入口（见 [安全边界](security-boundaries.md)）；`SSH_CREDENTIALS` 只引用操作员自己的密钥**文件路径**，不写私钥内容。
- Agent 永不执行修复；修复动作始终由人工在目标系统完成后回填修复事实。

**版本升级**

- `git pull` → `python -m uv sync --locked` → （`frontend/` 有改动时）重建前端 → 重启。
- 数据库迁移自动执行（仅新增表/索引，旧 `systems` 表升级前生成 `pre-v21` 备份，升级 schema 22 前生成 `<DATABASE_PATH>.pre-v22.bak` 一致备份，均不覆盖已有备份）。升级前仍建议先停止并完整备份 `./data/`。
- 恢复到旧版本时：停止所有 ATR 进程，把当前 `data/` 完整另存，用旧版本自己的数据库备份恢复到**新路径**，避免旧 WAL/SHM 混入；原数据保持原地，不自动删除。

## 七、WSL2

在 WSL 自己的项目副本和 Linux 虚拟环境运行，不复用 Windows `.venv`；建议数据位于 WSL 文件系统，避免两套 API 同时写同一 SQLite。准备 Python/uv 后：

```sh
python3 -m uv sync --locked
cd frontend && npm ci && npm run build && cd ..   # 仅 clone 方式
./scripts/atr init
./scripts/atr serve
```

`.env` 可选，规则与 Windows 相同。Windows 浏览器访问 `http://127.0.0.1:8000`；WSL 端口转发/防火墙依环境配置。本机通过 WSL Ubuntu 实际运行过 Docker Engine 与隔离 Linux/OpenSSH 夹具；WSL 原生 uv API 启动方式仍需在目标机器确认。

## 八、Docker

需要可运行的 Linux Docker Engine 和 **Compose ≥2.24**（api 的 `.env` 用 `required: false` 声明为可选文件）。默认不创建 `.env` 也能启动；只有启用 `docker-demo` profile 时必须提供至少 32 字符的 `COLLECTOR_TOKEN`，否则 Collector 启动即失败并给出明确错误。默认只启动 API/UI，不启动 Docker Collector：

```sh
docker compose up --build -d
```

访问 `http://127.0.0.1:8000`。数据库/资料在 `runtime-data` volume；API 容器不挂 Docker socket，上传 worker 在 API 容器内运行。镜像构建时会在容器内构建一次前端并把产物放到 `/frontend/dist`，因此宿主机不需要 Node。

要复验原 Docker Demo，仅在可信本地隔离环境显式启动 Collector：

```sh
docker compose --profile docker-demo up --build -d
```

Collector 具有 daemon 权限，`:ro` 不保证 Docker API 只读，不能接生产 socket。SSH 用容器部署时，凭据引用路径须是容器内管理员预先只读挂载的路径，当前 Compose 模板不自动挂载私钥。

V2.2 曾在 WSL Docker Engine 29.1.3 / Compose 1.29.2 实际构建、启动、浏览器接入并验证持久化与 A/B 故障恢复；Windows Docker Desktop 的 Linux 引擎当时未能启动，该路径仍需目标机器确认。若端口被占用，用 `ATR_HOST_PORT` 或为新项目指定独立 network/volume，不要停止别人的服务。旧版 docker-compose 需显式 `--env-file` 指向自己的 `.env`；模板顶层 `name` 使用现代 Compose。

## 九、已知注意事项（来自开发机实测）

- 直接 `uv run pytest` 会选错解释器，跑测试用 `python -m uv run python -m pytest`（需要 dev 依赖组：`uv sync --locked --group dev`）。
- 完整回归当前基线为 1089 passed、0 failed；部署机一般不需要跑全量测试，验收走第四节清单即可。
- 带中文注释的 PowerShell 脚本须含 UTF-8 BOM（仓库内脚本已满足，勿用记事本改动后另存为无 BOM）。
- Windows JobObject 分配失败会导致上传任务安全停止，须检查宿主进程限制，不能关闭资源边界继续。

## 十、参考文档

- [安全边界](security-boundaries.md) — SSH、Docker Collector 与只读边界
- [预算与模型配置](context-budget.md) — 预算、模型与用量
- [业务接入手册](business-onboarding.md) — 业务接入与 Demo 流程
- [Demo 手册](demo-runbook.md) — 双系统 Docker 演示步骤
- [README](../README.md) — 功能总览与安全声明
