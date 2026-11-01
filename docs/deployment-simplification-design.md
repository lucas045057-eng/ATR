# ATR 部署形态简化设计

状态：P0 已于 2026-11-01 实施（compose 单端口/单容器、本地可用默认值、token 按需校验、`/health` 诊断字段、部署文档合一）；P1–P3 未实施。下文「现状」一节保留为实施前的盘点记录。
范围：只改部署与启动路径，不改诊断运行时、证据 Gate、只读边界与调度规则。

---

## 一、现状盘点（有证据的复杂度来源）

### 1. 启动前必须手工完成 7~8 步，横跨两套工具链

当前 Windows 本地路径（`README.md` / `docs/deployment-new-machine-runbook.md`）：

1. `python -m pip install uv`
2. `python -m uv sync --locked`
3. `Copy-Item .env.example .env`
4. 手工把 `COLLECTOR_TOKEN` 改成 ≥32 字符随机值
5. `New-Item -ItemType Directory targets`
6. `cd frontend; npm ci; npm run build; cd ..`
7. `powershell -File scripts/run-local.ps1`

其中第 6 步是纯粹的**打包动作**，最终用户并不需要 Node.js —— 但 `frontend/dist` 在 `.gitignore` 里（`dist/`），clone 出来必然没有 dist，而 `scripts/run-local.ps1:8` 硬检查 `frontend/dist/index.html` 存在。README 说「只有重建前端时才需要 Node」，与事实相反：**clone 用户 100% 需要 Node**。这是当前最大的一处无谓门槛。

### 2. 配置默认值是容器路径，`.env` 从「可选」变成「必需」

`backend/app/settings.py:17-19` 的默认值：

| 键 | 代码默认 | 本机可用值 |
|---|---|---|
| `database_path` | `/data/runtime.sqlite3` | `./data/runtime.sqlite3` |
| `repository_roots` | `/demo,/repos` | `./demo,./targets` |
| `collector_url` | `http://collector:8090` | 本地通常不用 |

`get_settings()`（`settings.py:132-140`）还会校验每个 repository root 必须存在。所以 Windows 上不复制 `.env` 必然启动失败 —— 复制 `.env` 不是配置需求，而是**默认值放错位置**的补偿。

### 3. 强制 `COLLECTOR_TOKEN`，但只有 Docker Demo 才用它

`settings.py:84-87` 的 `validate_secrets()` 在 `get_settings()` 里**无条件**执行，要求 ≥32 字符且不能含 `replace-this`。而 `COLLECTOR_TOKEN` 只是 ATR → Docker Collector 的**出站凭据**，Collector 只在 `compose.yaml` 的 `docker-demo` profile 里启动。

结果：跑资料接入 + Mock 调查（产品主线，完全不碰 Docker）也必须先手工造一个 token。同时 `docs/deployment.md` 还写着「将**两个** token 改为不同随机值」，而 `.env.example` 里只有一个 —— 文档漂移。

### 4. Docker 形态三容器，其中一个是纯重复

`compose.yaml` 起 `api`（28000）、`web`（28080，nginx）、可选 `collector`。

但 `backend/app/main.py:118-120` 已经把 `frontend/dist` 挂在 `/` 上，**FastAPI 自己就是 Web 服务器**。`web` 容器做的事（静态托管 + 反代 `/api`）API 已经做了，代价是多一个容器、多一个端口、多一套 `node:22-alpine + nginx` 构建、多一处 CORS/反代配置面。

### 5. 三份部署文档 + 端口三套说法

- `README.md:32-60`：Python + uv，8000。
- `docs/deployment.md:61`：Docker 访问 **8080**，API 8000。
- `docs/deployment-new-machine-runbook.md:156`：Docker 起在 **28000 / 28080**。
- `compose.yaml` 实际：28000 / 28080。

同一件事三种说法，其中 8080 是错的（`web` 映射的是 28080）。用户无法判断哪份是事实源。

### 6. 发布打包链路与「本地部署」混在一起

`scripts/package_release.py` 要求：运行中的 `atr-runtime-api-1` 容器、`docker save`、精确 image id 与 config_id、SHA256SUMS、license notices 收集。这是**正式发版**工具，对单操作员本机部署没有任何帮助，但文档里与之并列出现，加重了「部署很复杂」的观感。

### 7. 小项

- 版本号漂移：`pyproject.toml` / `main.py` 写 `2.2.0`，文档写 V2.3。
- 无 CLI 入口：必须记住 `uvicorn app.main:app --app-dir backend --port 8000 --workers 1` 这条长命令。
- `/api/v1/health` 只返回 `{"status":"ready"}`，无法确认跑的是哪个版本、数据目录在哪、dist 是哪次构建。
- 无「一键体检」：端口占用、dist 缺失、目录不可写，都要到启动报错时才知道。

---

## 二、目标形态

**原则：一个产物、一条命令、一个端口；默认值本地可用，`.env` 只在需要外部模型 / SSH / Docker Collector 时存在。**

### 新用户旅程（目标：≤3 条命令，≤5 分钟，不需要 Node）

```powershell
# 方式 A：源码包 / clone
.\scripts\install.ps1          # 检查 uv → sync → 初始化 .env(自动生成 token) → 建目录 → 校验/构建前端 → 打印访问地址
.\scripts\atr.ps1 serve        # 启动，单进程单 worker，默认 127.0.0.1:8000

# 方式 B：Docker（单镜像，含已构建前端）
docker run --rm -p 127.0.0.1:8000:8000 -v atr-data:/data atr:latest
```

### 三层收敛

| 层 | 现在 | 目标 |
|---|---|---|
| 运行形态 | Windows 原生 / WSL2 / Docker(3 容器)，三套步骤 | 原生单命令；Docker 单容器（可选 collector profile） |
| 前端 | 每次 clone 都要 Node 构建 | 发布产物自带 `dist`；只有改 UI 的人才装 Node |
| 配置 | 复制 `.env` + 手改 token 才跑得起来 | 零配置可跑；`.env` 只写「我要接外部模型 / SSH / Collector」的差异项 |

---

## 三、分阶段方案

### P0 — 零行为变更的收敛（约 0.5 天，风险低）

> 只删重复、改默认值、合文档。不改任何诊断逻辑，不影响既有测试基线。

**P0-1 删掉 `web` 服务**
- `compose.yaml` 去掉 `web`；`frontend/Dockerfile`、`frontend/nginx.conf` 移入 `docs/` 或直接删除（保留在 git 历史）。
- 结果：Docker 只有 `api` + 可选 `collector`，端口只剩 8000，天然同源，不需要反代。
- 验收：`docker compose up -d` 后 `http://127.0.0.1:8000` 直接出工作台，`/api/v1/health` 通。

**P0-2 默认值改为「本地可跑」**
- `settings.py`：`database_path=./data/runtime.sqlite3`、`artifact_root=./data/artifacts`、`repository_roots=./demo`（`targets/` 由 `atr init` 按需创建后追加）、`collector_url=http://127.0.0.1:8090`。
- 注意 `get_settings()`（`settings.py:136-139`）要求每个 repository root 目录存在，所以默认值只能指向仓库自带的 `./demo`；`targets/` 不能在默认值里出现，否则 clone 用户照样启动失败。
- 容器侧不变：`compose.yaml` 已显式用 `environment` 覆盖成 `/data/...`、`/demo,/repos`、`http://collector:8090`。
- 验收：不复制 `.env`、不建 `targets/` 也能起；容器内行为不变。

**P0-3 `COLLECTOR_TOKEN` 改为按需校验**
- 从 `get_settings()` 移出 `validate_secrets()`，改到 **Collector 客户端构造处**（`backend/app/tools/registry.py` 的 `collector_url` 使用路径）校验。
- 安全前提（必须同时满足）：Collector 服务端仍强制校验 token；ATR 侧只有在确实发 Collector 请求时才要求 token 合法。不接 Collector 时该凭据无任何用途，不存在放宽攻击面。
- 验收：本地 Mock 全流程不配 token 可跑；`docker-demo` profile 下缺 token 仍启动失败。

**P0-4 文档收敛为一份**
- `docs/deployment-new-machine-runbook.md` 定为唯一事实源；`docs/deployment.md` 内容并入后删除，保留归档链接。
- `README.md` 的启动段压缩成「三条命令 + 一个链接」，不再重复整段步骤。
- 顺带修掉：8080/28080 矛盾、版本 2.2.0 与 V2.3 的说明、`.env` 两个 token 的旧说法。

**P0-5 `health` 返回可诊断信息**
- `GET /api/v1/health` 增加 `version`、`build`（前端 dist 的 mtime 或 ci 写入的 build id）、`data_root`、`decision_protocol`。只增字段，不改结构。

### P1 — 一条命令：`atr` CLI（1~1.5 天，中等风险）

新增 `backend/app/cli.py` + `[project.scripts] atr = "app.cli:main"`：

| 子命令 | 行为 |
|---|---|
| `atr init` | 不存在 `.env` 就生成（自动随机 token）；建 `data/`、`targets/`；检查 dist，缺失时提示「运行 npm run build 或用发布包」；打印下一步 |
| `atr serve [--port 8000] [--host 127.0.0.1]` | 单 worker 启动 uvicorn；固定 `--workers 1` 不给参数（避免破坏任务状态一致性） |
| `atr doctor` | 体检：python/uv 版本、dist 与版本一致性、端口占用、data 可写、`.env` 关键项、Docker 可用性、collector token（仅在启用时） |
| `atr backup [--out DIR]` | 停机一致备份：`sqlite3` backup API + artifacts 打包（复用 `scripts/backup_runtime.py` 的逻辑），替代手工文档步骤 |
| `atr version` | 打印代码版本、dist build、protocol |

- `scripts/run-local.ps1` / `run-local.sh` 退化为两行薄包装（`atr init` 检查 + `atr serve`），保留旧入口不破坏肌肉记忆。
- 验收：新机 `atr init && atr serve` 两条命令可用；`atr doctor` 能准确报出「端口占用」「dist 缺失」「目录不可写」三类故障。

### P2 — 用户不需要 Node（1 天）

- CI（`.github/workflows/ci.yml` 已有 `npm ci && npm run build`）在构建后把 `frontend/dist` 作为 artifact 附到 release；发布 zip 由 `scripts/package_release.py` 的一个 `--source-only` 分支产出（不要求运行中容器），含 dist + `install.ps1`。
- `frontend/package.json` 的 build 写入 `dist/build.json`（版本 + 提交 + 时间），`atr doctor` 比对代码版本，避免「跑着旧前端」的经典坑。
- 验收：把 zip 拷到一台只有 Python 的 Windows 机器，`install.ps1` 一条命令起服务。

### P3 — 交付形态二选一（1~2 天，可选）

- **A. Docker 单镜像（推荐做分发形态）**：`api` 镜像内置 dist，`docker run -p 127.0.0.1:8000:8000 -v atr-data:/data atr`。`compose.yaml` 只作为「collector demo 可选编排」保留。
- **B. 免环境 exe**：PyInstaller/Nuitka 打 Windows 单文件，解决「客户机器连 Python 都不让装」的场景。体积大、构建慢，仅在有真实需求时做。
- 保留 `scripts/package_release.py` 作为正式发版链路，但在文档里明确区分「发版」与「本机部署」。

---

## 四、验收标准（可测量）

| 指标 | 现在 | 目标 |
|---|---|---|
| 新机从零到可访问 | 7~8 步，2 套工具链，~15 分钟 | ≤3 条命令，1 套工具链，≤5 分钟 |
| 是否需要 Node | 是（必须） | 否（仅改 UI 时） |
| 是否必须手改配置 | 是（token + 默认路径） | 否（零配置可跑） |
| Docker 容器数 / 端口数 | 3 / 2 | 1 / 1（+可选 collector） |
| 部署文档数量 | 3 份且互相矛盾 | 1 份事实源 + 1 份发版说明 |
| 故障可诊断性 | 启动报错才知道 | `atr doctor` 主动报 |

## 五、明确不做（保留的复杂度及其理由）

1. **多 worker**：任务池 / 恢复语义依赖单进程，`--workers 1` 写死在 `atr serve` 里，不给参数。
2. **Collector 默认启动**：Docker socket 等价 daemon 权限，必须留 `docker-demo` profile 显式开启。
3. **SSH 目标默认关闭**：`SSH_ENABLED=false` 与主机公钥校验不动。
4. **上传/解包限额**：本地不限流会带来资源风险，继续保留。
5. **不引入反向代理/CDN/多租户**：单操作员本地工具的定位不变。

## 六、风险与回滚

| 风险 | 缓解 |
|---|---|
| 改默认值影响容器行为 | compose 已显式覆盖全部相关变量；改前用 `docker compose config` 比对 |
| token 校验后移被误解为放宽安全 | Collector 服务端校验不变；补一条单测：未配 token 时 Collector 客户端构造抛错 |
| 删 `web` 服务后有人依赖 28080 | 文档与 compose 同步改；需要反代的场景写进「可选部署」一节 |
| CLI 与 uvicorn 直调行为不一致 | `atr serve` 只做参数拼装，不复制任何启动逻辑；测试断言 argv |

回滚：全部改动集中在 `compose.yaml`、`settings.py`、新增 `cli.py`、文档，不触数据库 schema，`git revert` 即可。
