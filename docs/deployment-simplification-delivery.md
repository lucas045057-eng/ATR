# 部署简化交付说明（P0–P2）

状态：已完成并提交；P3（正式分发形态）未开工。
对应提交：`fd6fb07`（P0+P1）、`213ddf7`（P2）、`b0f1741`（构建卫生）、`2398fab`（e2e 跳过语义）。
设计依据：[deployment-simplification-design.md](deployment-simplification-design.md)；运维手册：[deployment-new-machine-runbook.md](deployment-new-machine-runbook.md)。

---

## 一、改了什么

### P0 —— 一个容器、一个端口、零配置可跑

| 项 | 改动 |
|---|---|
| 容器 | `compose.yaml` 删除重复的 nginx `web` 服务，只剩 `api`（`127.0.0.1:${ATR_HOST_PORT:-8000}:8000`）+ 可选 `collector` |
| 镜像 | `backend/Dockerfile` 增加前端构建阶段（复用已钉 digest 的 `node:22.23.3-bookworm-slim`），产物落到 `/frontend/dist`，与 `main.py` 的 `parents[2]/frontend/dist` 解析一致 |
| 默认值 | `settings.py` 默认改为 `./data/runtime.sqlite3`、`./data/artifacts`、`./demo`、`http://127.0.0.1:8090`：不复制 `.env` 也能启动 |
| 凭据 | `COLLECTOR_TOKEN` 从「启动即强制」改为「发 Collector 请求前校验」；纯 HTTP 工具不再需要它；三个 Docker 只读工具缺 token 返回 409 |
| 安全 | Collector 服务端拒绝「长度合规但含 `replace-this` 的占位 token」与过短 token（启动即失败，exit 3） |
| 可诊断 | `/api/v1/health` 增加 `version`、`build`、`decision_protocol`、`data_root`、`ui`；配置非法时启动直接报「ATR 配置无效，无法启动」 |
| 文档 | `docs/deployment-new-machine-runbook.md` 成为唯一部署事实源，`docs/deployment.md` 删除 |

### P1 —— 一条命令

`scripts/atr.ps1` / `scripts/atr` 提供子命令：

- `init`：生成 `.env`（随机 token）与 `data/`、`data/artifacts/`、`targets/`；不覆盖已有 `.env`；失败回滚本次新建文件。
- `serve`：单 worker 启动（写死），默认 `127.0.0.1:8000`；`frontend/dist` 缺失只警告。
- `doctor`：Python 版本、dist、前端指纹、端口、数据目录可写、`.env`、`COLLECTOR_TOKEN`、repository roots、Docker Engine/Compose；任一项 FAIL 退出码 1；支持 `--json`。
- `backup`：复用 `scripts/backup_runtime.py`，按配置的精确 `DATABASE_PATH` / `ARTIFACT_ROOT` 备份；配置非法或目标已存在直接拒绝。
- `version`：版本、构建、决策协议、工作台可用性。

`scripts/run-local.ps1` / `run-local.sh` 退化为转调 `atr serve`。

### P2 —— 用户不需要 Node

- `frontend/build.mjs` 产出 `dist/build.json`（version / commit / built_at）；只有 git 顶层目录等于项目根时才记录 commit。
- `scripts/build_release.py` 产出 `dist/atr-release-<date>.zip`：源码 + 已构建 `frontend/dist`，排除 `.env`/`data/`/`output/`/`orbital/`/`.git/`/`node_modules/`；dist 与 HEAD 不一致时拒绝打包（`--allow-stale-dist` 可绕过）；`install.sh`、`install.ps1`、`scripts/atr`、`scripts/atr.ps1`、`scripts/run-local.sh` 在包内保留 0755。
- `install.ps1` / `install.sh`：检查 Python ≥3.11 → 必要时装 uv → `uv sync --locked` → `atr init` → 打印下一步；不覆盖已有 `.env`。
- 构建卫生：`npm run build` 先清空 `dist/`，`build.mjs` 用 rename 而非 copy，历史资源不再堆积（包体 6.9 MB / 496 文件 → 3.1 MB / 451 文件）。
- CI 上传 `frontend/dist` artifact。

---

## 二、验收证据

| 项 | 结论 | 证据 |
|---|---|---|
| 全量回归 | **1107 passed / 936.96s / exit 0** | `orbital/output/pytest-final-b0f1741.log` |
| 静态检查 | `ruff check .` 全通过 | 多轮复核 |
| 无 `.env` 冷启动 | health 200（含 5 个新字段）、`/` 出工作台、单 worker | Pi、Kimi 各自起真实进程 |
| 安全不变量 | 无 token 可构造 `ToolRegistry`、`health.get` 返回 200、三个 Docker 工具 409 且 Collector 零命中；Collector 短/占位/空 token 均 exit 3；错误 Bearer 401 | Pi `orbital/output/pi-acceptance/REPORT.md` |
| Docker 容器路径 | `build api` 成功；容器 `Up (healthy)`；`/` 542 字节工作台；`ui=true`；运行 uid 10001；无 `.env` 时 `compose config` exit 0；`down -v` 清理干净 | Pi 实测（Docker 29.9.2 / Compose v5.6.1） |
| 发布包 | 必需文件齐全、禁止项为 0、关键脚本 `0o755` | Pi 与 Kimi 两轮独立核对 ZIP |
| 全新安装 | 解压 → `install.ps1` → `serve` → health/首页 200；重复安装不改 `.env` | Pi、Claude Code 各自端到端 |
| 备份 | 自定义 `DATABASE_PATH`/`ARTIFACT_ROOT` 备份正确；无 git 环境 `application_sha_kind=version`；配置非法拒绝 | Kimi 复测（含伪造旧库干扰项） |
| 文档可执行性 | 干净 clone 按 runbook 字面跑通；`doctor` 10 项全 PASS | Pi |

协作分工：Claude Code 开发 → Pi / Kimi 独立真实验收（Kimi 已按新指令回归「只解决疑难杂症」，后续测试交 Pi）→ 缺陷交回开发者修复 → 复测。

---

## 三、使用方式

**源码包 / 发布包（无需 Node）**

```powershell
# 解压后
.\install.ps1
.\scripts\atr.ps1 serve
```

**git clone（需要构建一次前端）**

```powershell
python -m uv sync --locked
.\scripts\atr.ps1 init
cd frontend; npm ci; npm run build; cd ..
.\scripts\atr.ps1 serve
```

**Docker（单容器）**

```sh
docker compose up --build -d          # 127.0.0.1:8000
docker compose --profile docker-demo up -d   # 需要真实 COLLECTOR_TOKEN
```

---

## 四、已知限制（未验证的部分，不算通过）

1. **GitHub Actions 未实际执行**：CI 的 dist artifact 上传只做了 YAML 结构验证。
2. **原生 Linux 完整安装未跑通全流程**：`install.sh` 只在 Git Bash 下验证；Linux 解压后的 0755 权限位已实测。
3. **P3 未开工**：Docker 单镜像已实际等价达成（单容器出工作台），但未发布镜像；免环境 exe 未做。
4. **Windows 备份跳过目录 fsync**：成功返回保证逐文件校验，但不承诺目录项抗突然断电。
5. **e2e 依赖浏览器**：缺 Chromium 时现在会 skip 并提示 `playwright install chromium` 或 `ATR_BROWSER_CHANNEL=chrome`（此前是 34 条误报 error）。

## 五、回滚

改动集中在 `compose.yaml`、`backend/Dockerfile`、`backend/app/settings.py`、`backend/app/tools/registry.py`、`backend/app/api/health.py`、`backend/app/cli.py`、`scripts/`、`tests/`、`docs/`，未触及数据库 schema。逐提交 `git revert` 即可；删除的 `frontend/Dockerfile`、`frontend/nginx.conf`、`docs/deployment.md` 可从 git 历史恢复。
