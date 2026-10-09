# ATR V2.1 开发交接：迁移到 Docker 电脑

交接日期：2026-10-09。适用对象：接手开发的工程师或编码 Agent。

仓库：[lucas045057-eng/ATR](https://github.com/lucas045057-eng/ATR)，分支 main。
V2.1 代码基准提交：[07c15677e7a26ed55caa4e845e4f125e39d1e486](https://github.com/lucas045057-eng/ATR/commit/07c15677e7a26ed55caa4e845e4f125e39d1e486)。本文及验收样例作为后续文档更新提交，应用代码基准不变。

## 1. 接手时最重要的结论

这是一份已实现的 V2.1 增量工程，下一步应先做 Docker/Linux 运行验收，再修复实际发现的问题。保留现有架构和已完成能力，不重新从零设计。

上一台电脑已验证 **67 项 pytest 全部通过、无跳过**，以及真实 Chromium 到本地 API 的接入流程、回环 SSH 协议、前端构建、Ruff、依赖锁与 Compose 配置。它是 Windows 原生测试结果。

**尚未验证：Docker 镜像实际构建和启动、A/B Demo 真实故障修复、真实模型 API、真实 Linux 网关/SSH 账号权限、WSL2 实际运行。** Docker 配置检查通过不代表这些已通过。接手后把新环境结果单独记录，不覆盖或混淆旧结果。

目前有两条不同的运行路线：

| 路线 | 入口 | 运行环境读取 |
|---|---|---|
| 原 Compose Demo | 系统图谱接入 system-a/system-b → 故障调查 | 显式 docker-demo profile 的 Collector |
| V2.1 陌生业务 | 上传资料 → 画像审核 → 服务器范围 → 故障调查 | Mock 或 SSH 固定工具 |

V2.1 runtime target 目前只有 mock/ssh，没有把新业务画像直接接到本机 Docker 的 connector。现有 Collector 用于原 Demo。若需要统一两条路线，应作为后续增量需求实现，不能宣称已有。

## 2. 已交付能力和代码入口

| 能力 | 代码入口 | 接手注意 |
|---|---|---|
| 安全资料导入 | backend/app/business/ingestion.py、worker.py、resources.py、static.py | ZIP/TAR 流式校验；不执行资料；Windows/Linux 资源边界须保留 |
| 画像/版本/审核 | backend/app/business/models.py、profile.py、service.py | 五类来源、expected_version、哈希复用、stale 知识和人工角色保留 |
| 源码检索 | backend/app/business/source_index.py | FTS5 仅路径/符号/导入/路由元数据，局部最多 160 行，校验摘要和版本 |
| 预算/模型 | backend/app/context/budget.py、model.py | 请求前原子预留；actual/estimate 分开；默认外部模型关闭 |
| SSH/工具策略 | backend/app/connectors/、scripts/ssh_gateway.py | 固定入口 atr-read-v1 + stdin JSON，固定主机公钥，双层白名单 |
| 范围和融合 | backend/app/system/scope.py、business_graph.py | 排名不授权，须人工确认绑定；冲突保留双方来源 |
| 调查/Gate | backend/app/agent/business_investigation.py、workflow.py、tools/business.py | 保留 LangGraph；静态/历史/离线结果不能单独 supported |
| 任务/API | backend/app/jobs.py、api/business.py、api/v1.py | 202 job + 轮询；单 API 进程；重启保留 interrupted 审计 |
| SQLite 升级 | backend/app/storage/database.py、migrations.py | 增量 schema 21，旧数据库升级前 .pre-v21.bak，不丢原历史 |
| 中文工作台 | frontend/src/main.tsx、business.tsx、api.ts | 原四页 + 接入/档案/范围；不把全文件读进 React 状态 |
| 原 Docker 读取 | collector/app/main.py、compose.yaml | Collector 具有 daemon 权限，仅可信隔离本地 Demo |

已修复基线 Docker Titles/Processes、Pattern 循环状态、重复/弱相关证据、敏感字段脱敏、角色名称猜测、等价图谱使 Pattern 失效、八服务接入限制和长期同步调查问题。详见 [审计记录](audit-v2.1.md)。

## 3. 新电脑准备

### 3.1 克隆最新代码

Windows PowerShell：

```powershell
git clone https://github.com/lucas045057-eng/ATR.git
Set-Location ATR
git switch main
git pull --ff-only
git log -2 --oneline
docker version
docker compose version
docker info
```

Linux/WSL2 同样使用 git clone，目录切换改为 `cd ATR`。已有仓库先确认没有未保存改动再更新，不使用 reset --hard 或 force push。

Docker Desktop 使用 **Linux containers**，确认 docker info 有 Server 信息。原电脑的 Windows 路径、缓存目录、代理端口和虚拟环境不要复制到新电脑。若下载失败，配置新电脑自己的网络/镜像源，不改依赖版本绕过锁。

只有运行 Docker 工作台时，宿主机不必安装 Python/Node；镜像负责安装和构建。编辑代码并跑完整自动测试时需要宿主机 Python 3.11+、Node/npm、uv 和 Chromium；当前没有单独的开发容器配置。

### 3.2 创建本地配置

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force targets | Out-Null
```

Linux/WSL2 对应 `cp .env.example .env` 和 `mkdir -p targets`。仅在尚无 .env 时复制，已有私人配置不要覆盖。

编辑两个不同、至少 32 字符的随机 OPERATOR_TOKEN/COLLECTOR_TOKEN。只装了 Docker 时可用以下临时官方 Python 容器生成两行令牌，分别写入自己的 .env：

```powershell
docker run --rm python:3.12-slim python -c "import secrets; print(secrets.token_urlsafe(32)); print(secrets.token_urlsafe(32))"
```

保留 EXTERNAL_MODEL_ENABLED=false、SSH_ENABLED=false，先验收本地链路。.env、私钥、原始业务资料/数据库不在 GitHub 或交付 ZIP 中，需要在新机器单独配置。示例 Demo 密码只是隔离夹具值。

## 4. 第一轮：构建和运行工作台

在项目根目录：

```powershell
docker compose config --quiet
docker compose build api web
docker compose up -d
docker compose ps
docker compose logs --tail 100 api web
curl.exe -fsS http://127.0.0.1:8000/api/v1/health
```

Linux/WSL2 将 curl.exe 改为 curl。访问 **http://127.0.0.1:8080**，输入 OPERATOR_TOKEN。默认只启动 api/web，不启动 Collector，也不挂宿主机 socket 到 API。

验收：容器健康/API health 成功、页面加载无错误、可创建空业务、无资料时没有虚假服务。Docker 构建会自行生成前端，不需要先在宿主机 npm build；但容器内构建不会在宿主机生成 frontend/dist。

数据库与上传资料在 `atr-runtime_runtime-data` 命名 volume 的 /data 中（当前 Compose name=atr-runtime）。API 容器非 root、单 workers；不要用多个 API 实例同时连接这个数据库。停止用 `docker compose down` 保留 volume；不要为解决报错删除 volume、加 -v、删除 SQLite 或已有上传资料。

## 5. 第二轮：V2.1 完整接入冒烟

使用仓库中的 [catalog-onboarding.zip](fixtures/catalog-onboarding.zip)。它来自现有测试的 5 文件 Catalog 夹具，**仅用于静态接入与明确 Mock 流程，不是可部署的真实业务**；无需安装或运行里面的代码。

1. 新业务接入：创建 Catalog，上传样例，资料集 source；看到处理完成、5 个文件、模块/路由/服务骨架。
2. 业务档案：检查 catalog-api/application、store/datastore、依赖来源和未知项。搜索 record，读取局部源码，核对路径/SHA256/版本。审核并保存档案。
3. 服务器范围：创建 Mock/mixed，允许业务目录 /srv/catalog，保持 Docker 读取关闭。轻量发现后应显示 catalog-api 与无关对象；确认 catalog-api 的 service 绑定，不确认 unrelated 对象。
4. 故障调查：选该业务关联系统，症状 catalog-api startup failure，启动只读调查。离线模型下应完成任务并保持 unresolved，展示 mock 来源、实际工具/数据量和未解决原因。
5. 再次导入同 slot 的 replace、修改服务角色、查看历史，确认已有审核和失效知识没有被整体清空。失败上传不应破坏上一版画像。

这一步验证 **Docker 中 API/worker/SQLite/UI** 的新链路，运行观测仍为 Mock。不能记录为真实 SSH 或真实业务根因通过。图片/PDF 当前应 pending_manual；提供人工录入而不是虚构解析内容。

## 6. 第三轮：原 Docker Demo 实际运行

仅在可信本地开发电脑启动 Collector。socket 的 :ro 挂载不能限制 Docker daemon 写能力，不能换成生产服务器 socket。ATR Agent 的工具仍不含故障注入和修复。

```powershell
docker compose --profile docker-demo up --build -d
docker compose --profile docker-demo ps
docker compose -f demo/system-a/compose.yaml -p atr-system-a up --build -d
docker compose -f demo/system-b/compose.yaml -p atr-system-b up --build -d
curl.exe -fsS http://127.0.0.1:18080/health
curl.exe -fsS http://127.0.0.1:18080/record
curl.exe -fsS http://127.0.0.1:18081/health
curl.exe -fsS http://127.0.0.1:18081/record
```

先启动根工作台以创建 atr_runtime 网络，再启动 A/B。端口为 8080、8000、18080、18081。API 容器通过 http://edge_proxy/health 和 http://ingress_proxy/health 访问演示健康地址；宿主机使用 18080/18081。不要把容器中的 127.0.0.1 当宿主机。

“系统图谱”接入 source_key=system-a/system-b、compose_file=compose.yaml。Compose project 必须分别为 atr-system-a/atr-system-b，命令中的 -p 保持不变，否则 Collector 无法正确匹配。

以下故障操作由操作者针对本地 Demo 执行，不能移到未授权业务服务器：

```powershell
docker compose -f demo/system-a/compose.yaml -p atr-system-a stop mysql
docker compose -f demo/system-a/compose.yaml -p atr-system-a restart python_api
curl.exe -i http://127.0.0.1:18080/health
```

记录故障发生时间和实际 HTTP/日志/状态，再在系统 A 创建故障调查（离线时仍应 unresolved）。预期现象是应用启动检查失败、代理 502；如果现象不同，先调查真实输出，不照写预期结果。

人工恢复：

```powershell
docker compose -f demo/system-a/compose.yaml -p atr-system-a up -d mysql python_api
curl.exe -fsS http://127.0.0.1:18080/health
curl.exe -fsS http://127.0.0.1:18080/record
```

等待数据库健康；若应用未恢复，查看日志再决定操作。修复后健康和业务 record 都应验证。真实模型接入后，再验证 supported → 人工修复记录/实际健康 GET → Candidate → 人工审核 → Trusted。不能靠修改 Gate 或种入 Pattern 做出成功结果。系统 B 与配对评估见 [原 Demo 手册](demo-runbook.md)，效率只能报告真实记录。

## 7. 在新环境重跑自动测试

GitHub 不保存 frontend/dist/node_modules/.venv。完整浏览器测试必须在宿主机另行构建前端，即使 Docker 页面已经能打开。

Windows 开发环境：

```powershell
python -m pip install uv
python -m uv sync --locked --group dev
Set-Location frontend
npm ci
npm run build
Set-Location ..
python -m uv run playwright install chromium
New-Item -ItemType Directory -Force test-results | Out-Null
python -m uv run pytest -q --junitxml=test-results/new-machine.xml
python -m uv run ruff check backend collector scripts tests
python -m uv lock --check
```

Linux/WSL2 使用 python3、cd、mkdir -p；Chromium 的系统库由开发电脑操作者按本机环境安装，不在真实业务服务器自动安装。缺少 dist 或 Chromium 时 E2E 可能 skip，必须明确报告，不能说“67 项均通过”。生产 API 镜像没有 dev 依赖/测试目录，不能直接以其 pytest 代替完整测试环境。

新机报告至少记录：OS/CPU 架构、Docker/Compose 版本、提交 SHA、执行命令、通过/失败/跳过数、容器状态、页面结果、故障/恢复时间、run/evidence ID、模型 usage 类型。日志/截图先脱敏；原始大日志和私人数据留 test-results/ 或受控本地目录，不提交。文档中的测试 stub usage 不能当真实供应商计费。

## 8. 下一阶段开发优先级

| 优先级 | 任务 | 完成标准 |
|---|---|---|
| P0 | 当前 Docker 镜像/worker/卷权限/网络/UI 验收 | 实际构建启动、上传解析、持久化、原 A/B 健康、故障和恢复都有记录；修复真实问题后回归 |
| P1 | 真实模型接入与预算 | 实际 JSON/usage，离线/失败不伪造，低预算不发送请求，费用标明估算/未知；不全文发送源码 |
| P2 | 隔离 Linux SSH 运行测试 | 可新增独立 opt-in Docker SSH 测试夹具，不挂宿主机 Docker socket；验证 forced command、openat、systemd/journal 可用性、越界/链接/超时/断连和审计 |
| P3 | 已授权 Linux 测试主机 | 所有者明确授权、可信核对主机公钥、受限账号/策略确认，再做轻量发现→审核范围→定向读取；不自动安装/提权 |
| P4 | 业务能力扩展 | JS/TS 语义、媒体适配、增量影响子图、资料保留/清理策略；需要本地 Docker 新业务 connector 时单独设计 |

P2 的 Linux SSH 完整夹具目前未提供；现有 test_ssh_protocol.py 是回环协议服务器，只证明握手/主机密钥/固定 JSON/超时，不能证明真实 Linux 网关权限。实现新夹具前区分隔离容器配置与真实主机操作。

模型默认值：初始输入 8,000、调查每请求输入 12,000、初始调用 6、调查模型轮数 8、调查工具 20；业务页面和 .env 均可配置。SSH 默认关闭，UI 仅填 credential_ref 与完整主机公钥，私钥文件映射由操作者提供。容器内凭据引用必须指向管理员预先只读挂载的容器路径，Windows 主机路径不能直接使用。参见 [预算](context-budget.md)、[安全](security-boundaries.md)。

## 9. 接手时须保留的边界和限制

- 固定工具/参数 Schema/Policy + 实际账号权限；不增加任意 shell 执行器，不让模型拼接命令，不自动提权或修复。
- 不扫描 /，配置仅具体授权目录的单层清单/有限文件。候选分数仅排序，未确认绑定不深读 service/container。
- 来源仍区分 Documented/Static/Runtime/Inferred/Human Verified，资料旧版本不能当当前运行证据；冲突保存双方事实。
- Trusted 必须当前证据诊断、人工修复记录、真实验证和人工审核；历史 Pattern 只建议检查。Gate 不能证明任意自然语言因果判断正确。
- 图片/PDF 自动解析未实现；JS/TS/动态关系不完整；FTS 不是正文全文检索；增量解析复用但画像骨架仍本地重建。
- 删除是逻辑停用，历史/失败文件仍占盘；无自动物理清理。单 API 进程、单操作员，无多租户/RBAC/外部任务队列。
- 不推倒重写、不清空数据、不通过 fake 响应/种入成功率完成验收。只有用户要求时才发布；不提交 .env、私钥或私人原始资料。

## 10. 遇到问题先看哪里

| 现象 | 排查方向 |
|---|---|
| Docker 连接失败/无 Server | Docker Desktop Linux engine、WSL 集成、当前 context；先修环境 |
| build 下载超时 | 新电脑网络/代理/镜像源、uv/npm 锁；不要复制旧机代理端口 |
| API 退出或 401 | 两个 token 是否已替换、根目录是否存在、工作台 token 是否匹配 |
| 上传 job failed | API 日志和 job.error、/data 目录权限、Linux worker 内存/耗时、大小/类型/路径；保留限额 |
| 页面可用但 E2E skip | 宿主机 frontend/dist 或 Chromium 缺失，与容器内 dist 不同 |
| Demo external network 不存在 | 根工作台先启动以建立 atr_runtime |
| 宿主机正常但 API 健康验证失败 | 容器 DNS/网络、ALLOWED_HEALTH_URLS 精确地址；容器 localhost 不是宿主机 |
| 未确认范围就 denied | 按设计先审核绑定；不要绕过 Policy |
| 有 Docker 仍没有 SSH 真实观测 | Docker Collector 与 V2 runtime target 是两条路线；SSH 还需要受限网关/账号 |
| 模型已填但仍离线 | EXTERNAL_MODEL_ENABLED、JSON 声明、model/key/base URL、容器重建配置、预算 stop_reason |

修复问题要保存具体错误、最小复现和对应回归测试；验证通过后更新 [验收报告](acceptance-report.md) 与 [能力状态](implementation-status.md)，只把真实运行项目标为通过。

## 11. 可直接交给下一位编码 Agent 的任务

> 请接手 lucas045057-eng/ATR 的 main 分支，以 docs/development-handoff.md 为交接依据。V2.1 应用代码基准为 07c1567，已有 67 项 Windows 离线/隔离自动测试通过，但 Docker、真实模型、Linux 网关尚未运行验收。
>
> 当前是在有 Docker 的开发电脑上继续。先读 README、交接文档、架构、安全、验收报告，再检查代码和环境。优先实际构建并启动默认 API/UI，上传 docs/fixtures/catalog-onboarding.zip 完成 Mock 接入链，然后用显式 docker-demo profile 验证原 A/B Demo 的健康、故障与人工恢复。区分原 Collector 路线和 V2 Mock/SSH，不假定它们已统一。
>
> 发现问题后做最小增量修复并跑对应回归、完整前端/E2E 与预算/安全测试，保存新机实际结果；不要重写架构或虚构通过。没有真实模型凭据时保留离线结果；没有明确真实主机授权时只用 Mock/隔离 Docker/回环 SSH。固定工具、主机公钥、预算、人工范围审核、Evidence Gate、Trusted 的修复与人审条件必须保留。
>
> 然后提出并实现可重复的隔离 Linux SSH 网关测试夹具；真实服务器接入须由所有者明确授权，不自动安装/修改服务器/提权。不要删除已有数据、提交私钥或 .env，也不要未经要求 push、创建 PR 或 Release。阶段结束报告实际通过/失败/跳过、未验证项目和下一步。

## 12. 配套资料

[启动](../README.md) · [架构](v2.1-architecture.md) · [接入手册](business-onboarding.md) · [API](api-v2.1.md) · [部署/恢复](deployment.md) · [安全](security-boundaries.md) · [预算](context-budget.md) · [验收](acceptance-report.md) · [原测试结果](test-results.xml) · [原 Demo](demo-runbook.md)
