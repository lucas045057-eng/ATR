# 本地部署、升级与恢复

## Windows 原生（本机已验证路径）

Python 3.11+，需要重新构建前端时使用 Node/npm；V2.2 本机验证锁定 Python 3.12.15、Node 24.14.1，镜像构建使用 Python 3.12 / Node 22。ZIP 含已编译前端，依赖安装仍需要网络/可用包缓存。项目目录中执行：

```powershell
python -m pip install uv
python -m uv sync --locked
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
New-Item -ItemType Directory -Force targets | Out-Null
```

将两个 token 改为不同的随机私密值（至少 32 字符，示例占位值会被拒绝）。可在 PowerShell 用以下方式分别生成，生成结果仅用于自己的 .env，不提交或粘贴聊天：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

默认 .env 的 DATABASE_PATH/ARTIFACT_ROOT 使用项目相对路径，REPOSITORY_ROOTS=./demo,./targets；配置根目录须存在。原 V1 通过已克隆 demo/targets 接入，V2 可直接上传，不自动从外部 URL 克隆。

重建前端（ZIP 内已有 dist 时可跳过）：

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-local.ps1
```

已有 dist 时直接执行最后一条启动命令，访问 http://127.0.0.1:8000。入口只启动单 workers API；禁止配置多个 workers 共用当前本地任务池，否则重启恢复与活动任务状态可能互相干扰。Ctrl+C 停止，重启保留数据库和资料。

若 npm 默认全局 cache 没权限，可配置自己项目的缓存目录再安装，无需管理员权限。若 Windows JobObject 分配失败，资料任务安全停止，需要检查当前进程限制/宿主环境，不能关闭资源边界以继续。

## WSL2

在 WSL 自己的项目副本和 Linux 虚拟环境运行，不复用 Windows .venv；建议数据位于 WSL 文件系统，避免两套 API 同时写同一 SQLite。准备 Python/uv/node 后：

```sh
python3 -m uv sync --locked
test -f .env || cp .env.example .env
mkdir -p targets
cd frontend
npm ci
npm run build
cd ..
sh scripts/run-local.sh
```

编辑两个 token 后再启动。Windows 浏览器访问本地 8000；WSL 转发/防火墙依环境配置。本机通过 WSL Ubuntu 实际运行 Docker Engine 与隔离 Linux/OpenSSH 夹具；未直接运行 WSL 原生 uv API，这条启动方式仍需在目标机器确认。

## Docker 可选部署

需要可运行的 Linux Docker Engine。`.env` 与 targets 准备完成后，默认启动 API/UI，不启动 Docker Collector：

```sh
docker compose up --build -d
```

访问 http://127.0.0.1:8080，API 8000。数据库/资料在 runtime-data volume；API 不挂 Docker socket，上传 worker 在 API 容器内运行。要复验原 Docker Demo，仅在可信本地隔离环境显式启动 Collector：

```sh
docker compose --profile docker-demo up --build -d
```

Collector 具有 daemon 权限，`:ro` 不保证 Docker API 只读，不能接生产 socket。[原 Demo 手册](demo-runbook.md) 包含 A/B 人工故障、修复及负例步骤。SSH 用容器部署时，凭据引用路径须是容器内管理员预先只读挂载的路径，当前 Compose 模板不自动挂载私钥。

V2.2 已在 WSL Docker Engine 29.1.3 / Compose 1.29.2 实际构建、启动、浏览器接入、持久化以及 A/B 故障恢复。为避开已有服务，验收使用单独的 atr-v22-validate 项目、28000/28080 端口和独立 network/volume，未修改已有项目。Windows Docker Desktop 的 Linux 引擎本次未能启动。新机器默认按上面现代 docker compose 启动；若端口占用，先为新项目明确指定自己的端口和网络，不停止别人的服务。

本次 Docker Hub DNS/TLS 错配使用 Docker 官方 ECR Public 中相同版本镜像，未关闭 TLS 验证。原 Dockerfile 版本保持不变，目标机器仍需能获取这些基础镜像。本机使用旧 docker-compose 时需显式 --env-file 指向自己的 .env；模板的顶层 name 使用现代 Compose。源镜像目录见 [Docker 官方公共库](https://gallery.ecr.aws/docker/)。

## 模型、SSH 与预算

默认离线可完成上传/解析/审核/Mock 发现/调查的未解决链路。模型配置后须显式 EXTERNAL_MODEL_ENABLED=true；不需要填写真实 key 来跑测试。真实 SSH 须先经所有者授权并配置服务器强制命令，参见 [安全边界](security-boundaries.md)。预算见 [配置说明](context-budget.md)。

## SQLite 升级与恢复

迁移仅新增表/索引/五个绑定时效字段，schema_migrations 记录 21/22。检测旧 systems 表且无迁移表时生成 pre-v21，升级 schema 22 前生成 `<DATABASE_PATH>.pre-v22.bak` 一致备份，均不会覆盖已有备份。故障、Pattern、运行历史仍在原表；知识内容 schema 2 使用既有 JSON 字段，不等同数据库 schema 23。测试覆盖保留旧系统记录与幂等初始化。

升级前仍建议操作者停止旧 API，保存完整数据库与资料目录的独立备份。不能只在运行中复制 runtime.sqlite3 而忽略 WAL；可用 sqlite3 backup 生成一致副本。自动 pre-v21 备份只含数据库，不包含原仓库/资料文件，也不会周期更新。

恢复到 V1 的操作方案：停止所有 ATR 进程，将当前 data 目录完整另存，再把 pre-v21.bak 复制到新的恢复目录下命名 runtime.sqlite3，在 V1 代码的 .env 中指向此新数据库，按旧配置启动。原 data、上传资料与 V2.1 历史保持原地以便后续恢复，不做自动删除。新路径避免旧 WAL/SHM 混入恢复库。V1 无法显示 V2 业务画像是版本能力差异。

若仅恢复 V2.1，采用自己的一致性数据库备份与对应时刻资料目录的新副本，配置新路径后启动。不要直接覆盖运行中的数据库。Docker volume 恢复须由操作者停止容器并按自己的 volume 备份方案完成，本机未执行这些恢复操作。

## 验证命令

```powershell
python -m uv sync --locked --group dev
python -m uv run playwright install chromium
python -m uv run pytest -q --junitxml=docs/test-results.xml
python -m uv run ruff check backend collector scripts tests
python -m uv lock --check
```

浏览器测试需前端 dist 和 Chromium，缺失时可能显式 skip。安装浏览器/开发依赖属于 ATR 工程，不会安装上传业务的依赖。测试只用临时数据、本地回环 HTTP/SSH、显式 Mock；不访问已知真实业务服务器。

本机 Windows 临时目录权限受限时，使用每次新建的项目内 --basetemp；本次浏览器下载 CDN 失败时显式 ATR_BROWSER_CHANNEL=chrome 复用已安装 Chrome。完整默认测试、18 项实际隔离 SSH 及 78 次故障比较的命令见 [V2.2 隔离验收手册](v2.2-validation-runbook.md)。
