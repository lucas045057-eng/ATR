# ATR V2.2 普通 SSH 只读兼容开发报告

日期：2026-10-10。项目：`E:\ATR`。本地分支：`fix/litellm-runtime-isolation`。

## 1. 交付范围与操作者最新要求

本轮基于已有本地未提交成果开发，没有用 GitHub 代码覆盖工作区。

操作者在任务书之后明确调整了要求：

- 所有 ATR 已注册只读工具均允许，不再限制默认三项或建立额外隔离审批流程。
- 普通 SSH 允许 AI 连续调用已授权只读工具。
- 新建目标默认开启 Docker **只读工具权限**，页面默认勾选，可关闭。

因此，任务书最初的“普通 SSH 禁止 AI 自动调用、逐次人工确认、Docker 默认关闭”不作为本次最终行为。服务映射人工审核、资料目录范围、主机公钥校验、低权限身份检查、工具白名单、预算和独立证据门仍保留。启用 Docker 读取不代表已获得 Docker daemon 的访问权限。

## 2. 原架构审计与兼容方案

原运行工具入口是 `RuntimeService.execute`，先验证固定工具及范围，再选择 Mock 或 Paramiko 连接器。原 SSH 仅发送固定命令 `atr-read-v1`，工具参数通过 JSON stdin 传入服务器网关。服务映射、运行观测、图谱、工具审计和 Agent 证据门已经存在。

本轮只增加普通 SSH 执行适配器，复用原 Paramiko 认证和通道读取代码，没有另建诊断系统、凭据系统或隔离审批工作流。

| 执行方式 | 远程执行 | 安全声明 | AI 调用 |
|---|---|---|---|
| `forced_gateway` | 固定 `atr-read-v1` + JSON stdin | 服务器配置的强制网关；须由管理员验收 | 保留原授权调查能力 |
| `standard_ssh_readonly` | ATR 固定命令目录 | 客户端约束；不宣称服务器强制只读 | 允许既有工作流调用授权只读工具 |

旧 SSH 配置缺少 `execution_mode` 时仍解释为 `forced_gateway`，不会自动降级。`auth_mode` 与执行方式独立，四种组合均可配置。目标连接配置继续保持不可直接编辑：更换方式需要创建新目标、重新声明授权和重新审核绑定，旧绑定不会移植到新目标。

## 3. 修改文件

| 文件 | 本轮内容 |
|---|---|
| `backend/app/connectors/standard_ssh.py` | 普通 SSH、身份检查、13 个固定读取工具、结构解析、受限文件读取程序 |
| `backend/app/connectors/ssh.py` | 提取共享认证连接和有界通道读取；保留固定网关协议；容器日志保留 stderr |
| `backend/app/connectors/models.py` | 执行方式、客户端权限声明；新目标 Docker 只读默认开启 |
| `backend/app/connectors/policy.py` | 普通 SSH 数量类型和首尾空白校验；复用既有范围授权 |
| `backend/app/connectors/service.py` | 选择执行适配器、两种方式复用密码会话、返回固定安全失败提示 |
| `backend/app/system/scope.py` | 普通 SSH 发现遇到失败停止；单次成功观测融合到既有图谱 |
| `backend/app/system/business_graph.py` | 已采集普通 SSH 主机的观测来源和身份预检查；明确未验证服务器端只读 |
| `frontend/src/business.tsx` | 执行方式、认证、边界说明、限额、全部固定工具、失败信息、模式改变清空新建授权声明 |
| `frontend/src/styles.css` | 修正授权和 Docker 复选框排版 |
| `tests/test_standard_ssh.py` | 配置兼容、注入拒绝、身份检查、图谱及 Fake Provider Agent 边界 |
| `tests/e2e/test_standard_ssh_ui.py` | Chrome 验证四种组合、Docker 默认、密码不存浏览器、模式改变重新声明 |
| `tests/e2e/test_browser.py` | 默认启用容器发现后，原 Mock 流程明确选择首个待审核候选 |
| `tests/integration/standard_ssh_acceptance.py` | 真实隔离 OpenSSH 读取、错误认证、主机公钥、权限、限额、文件及客户端边界 |
| `tests/fixtures/linux-ssh/Dockerfile`、`start.py`、`readonly_cli.py` | 仅测试容器内普通低权限/高风险组账号、合成 CLI、延时及超量输出端点 |
| `scripts/verify_ssh_password.py` | 原密码验收脚本增加 `--standard`，复用隔离项目和合成凭据管理 |

此前独立模型 Worker、模型错误治理、结构化输出兼容、密码会话等本地成果完整保留。本轮没有修改模型、Base URL、授权配置、模型 Schema、Prompt、推理档位或数据库结构。

## 4. 认证、身份与凭据生命周期

两种执行方式共用一个认证实现：完整主机公钥固定校验、`RejectPolicy`、禁止 SSH agent 和默认私钥搜索。密码仅显式 `auth_password(..., fallback=False)`，不会切换其他认证方法；私钥仍使用后端已有凭据引用。

普通 SSH 每次工具调用在同一连接中先运行六个固定身份读取命令：`uname -s`、`id -u`、`id -un`、`id -g`、`id -G`、`id -Gn`。核验 Linux、实际用户名、UID、主组及附加组，拒绝 UID/GID 0、root、sudo/wheel/admin/docker/lxd/lxc/disk/shadow/libvirt 等明确高风险组，以及不可解析的身份结果。检查成功仅标记“身份预检查通过”；不是 sudoers、ACL、polkit 或全部文件权限审计。

密码仍在可信后端进程内存保存，固定 30 分钟，不滑动续期。数据库只保存随机会话引用。提供、更新、清除仍走原专用 API；认证失败或主机公钥错误清除对应引用。过期在连接前、通道发送前和接收循环中持续检查。后端重启清空密码，私钥引用仍可使用。没有密码落盘、普通配置字段、浏览器 localStorage/sessionStorage 或模型 Worker 凭据传递。Python 字符串回收不保证取证意义上的字节覆盖；不宣称已实现安全内存擦除。

## 5. 固定只读命令目录

所有命令由 ATR 代码决定，用户和模型仅能提交已有工具及受校验参数。

| 工具 | 固定命令及限制 |
|---|---|
| `host.info` | `/usr/bin/uname -s -n -r -m` |
| `host.memory` | `/usr/bin/free -b` |
| `host.disk` | `/usr/bin/df -P -h` |
| `process.list` | `/usr/bin/ps -eo pid,comm --no-headers --sort=pid`；客户端有界排序和分页 |
| `network.listeners` | `/usr/bin/ss -lntu`；不读取进程参数或凭据 |
| `service.list` | `systemctl list-units --type=service --all --no-pager --no-legend --plain --full` |
| `service.status` | `systemctl show --no-pager --property=ActiveState,SubState,Id -- <已审核服务>` |
| `service.logs` | `journalctl --no-pager --output=short-iso -n <1..200> -u <已审核服务>` |
| `container.list` | `docker ps --all --no-trunc --format '{{json .}}'`；只读取固定清单字段 |
| `container.status` | `docker inspect --format '{{.State.Status}}' <已审核容器 ID>` |
| `container.logs` | `docker logs --timestamps --tail <1..200> <已审核容器 ID>`；包含 stdout/stderr |
| `config.list` | 已有 `/usr/bin/python3 -I -c <ATR 固定只读程序>`；JSON stdin；仅授权目录单层常规文件，最多 100 个 |
| `config.read` | 同一固定程序；逐段 `openat`/`O_NOFOLLOW`/`O_NONBLOCK`；仅授权范围内有界常规 UTF-8 文件 |

不会安装远程软件。基本工具须已存在于上述受信任系统路径。文件工具需要目标已有 `/usr/bin/python3`；缺少解释器、systemd、journalctl、Docker CLI 或相应读取权限时安全失败，不安装软件、不提升权限、不替换执行方式。目录超过 100 个文件时失败，不返回冒充完整的目录清单。

## 6. 注入、范围和资源限制

动态服务标识、容器 ID、分页参数继续使用既有 Pydantic Schema 和授权 Policy。普通 SSH 额外拒绝数量类型转换和首尾空白。服务/容器深入读取仍须操作者审核；配置路径须在明确业务目录下，拒绝全盘范围、目录穿越和符号链接。文件路径作为 JSON 数据，不拼入 Shell。

普通工具使用绝对路径及 `shlex.join` 固定参数，不提供用户脚本、任意环境变量、管道、重定向、SFTP、SCP、sudo、端口转发或任意命令入口。OpenSSH exec 仍可能经用户 Shell 解释；不能称为远程 `shell=False` 隔离。

连接、认证及 banner 有原超时，普通采集的六项检查和实际工具共享执行 deadline 与输出预算。超时/超量输出关闭通道，失败只保存审计类型，不生成成功证据。无自动重试、自动切换模式或提权。工具次数上限沿用原原子计数及观测审计。密码、公钥、身份失败和普通发现中的其他失败停止该发现操作。

## 7. Agent、证据和授权集成

按操作者最新要求，没有增加普通 SSH 自动动作拦截或逐次确认界面。现有 Agent 通过 `BusinessToolRegistry` 调用同一 `RuntimeService` 和固定命令目录；模型不能提交任意 Shell 或替换工具参数。Fake Provider 测试验证已授权普通 SSH 状态/日志/配置动作可以执行，未授权 store 不出现在候选动作中，注入伪造 action 被 `model_schema_error` 拒绝，不产生对应 SSH 执行。

继续使用原运行观测库、图谱、工具调用审计、证据 Store、业务画像、假设生命周期、Token 预算和独立证据门。单次人工读取及发现均可把普通 SSH 主机观测来源写入原图谱，并明确 `server_enforced_readonly=false`。失败不作为根因，权限检查不能被模型文本覆盖。

既有系统区分“人工确认的读取授权”与“映射观测的新鲜度”：人工授权仍保持原规则，Agent 的实时映射继续由原新鲜度门控制。本轮不改变这一既有约定。

## 8. 前端使用

在“服务器范围”选择 SSH 和普通 SSH 只读兼容，填写主机、端口、低权限账号、已核验完整主机公钥，选择密码或私钥引用，声明主机和资料授权范围。密码方式不需要私钥路径。可设置超时、累计调用次数和输出上限；Docker 只读默认勾选，可关闭。

新建表单改变执行方式会清空权限声明和授权勾选；保存的目标不支持直接切换方式。身份通过和操作者声明分别展示，普通 SSH 始终不显示“服务器端强制只读已验证”。真实读取失败显示安全类型/固定提示，密码不回显。SSH 总开关仍保留现有配置；本轮未擅自修改本地 `.env`。

## 9. 验收证据

最终结果（零失败、零错误、零跳过）：

| 验证 | 结果 | 证据 |
|---|---|---|
| 完整回归 | **378 passed，535.91 秒** | `test-results/standard-ssh/full-frozen.xml`、`full-frozen.log`、`full-frozen.exit=0` |
| 隔离 OpenSSH + Docker Mock + Chrome | **67 passed，26.096 秒**；1 项模块预导入告警，无跳过 | `test-results/standard-ssh/acceptance-ecd6c2a7/junit.xml`、`summary.json`、`tests.log` |
| 最终单元/范围回归 | **81 passed，36.46 秒** | `test-results/standard-ssh/unit-frozen.xml` |
| 最终页面排版与四种组合 | **1 passed，4.03 秒** | `test-results/standard-ssh/ui-final.xml`；截图位于 `ui-final/test_execution_and_auth_choice0/standard-ssh-modes.png` |
| 静态校验/前端构建/镜像构建/差异空白检查 | 全部通过 | 同目录 `ruff`、`frontend-build`、`api-build`、`web-build`、`fixture-build-final`、`diff-check` 日志及退出码 |

完整回归比上一阶段 325 项新增 52 项普通 SSH 单元测试和 1 项页面测试。隔离验收为原 32 项网关测试、32 项普通 SSH 测试、3 项页面测试。另有脚本断言验证异步发现、非法 Shell 入口、资料脱敏、密码不入数据库/日志/环境/证据文件、重启丢失密码且保留私钥引用，以及普通 SSH 主机观测入图谱。

初始模拟命令执行权限/Python 路径配置错误、Docker 默认勾选造成旧页面多候选定位错误均已修正；范围回归发现新鲜度与授权语义的兼容问题后已恢复原约定。中途失败/中断目录保留，最终以 `full-frozen` 和上述隔离目录为准，不以中途结果冒充通过。

已确认的测试覆盖：两种认证和执行方式、旧目标默认网关、所有固定只读命令、非法参数/注入拒绝、UID 和高风险组、会话失效、真实通道超时/输出上限、描述符安全文件读取、stdout/stderr、普通账号在 ATR 之外仍能写自己的目录，以及原 32 项服务器强制网关测试。

**边界区分：**uname/free/df/ps/ss、身份检查和文件读取在真实 Linux/OpenSSH 容器执行。systemctl/journalctl/docker 使用明确的合成 CLI，容器内没有运行 systemd 或真实 Docker daemon；这些测试证明命令构造、SSH 传输、解析及授权，不证明生产 Docker 权限已配置或实际业务服务状态。未连接正式服务器，真实付费模型调用为 0。

## 10. 本地部署和数据保留

本地 API 已更新为 `atr-standard-ssh-api:20261010`，Web 为 `atr-standard-ssh-web:20261010`；API `127.0.0.1:28000` 和 Web `127.0.0.1:28080` 返回 HTTP 200，API 容器 healthy。仅重建本地 API/Web，隔离验收项目已经停止；未启动正式 collector 或对真实 Docker daemon 发起业务读取。

复用 `atr-runtime_runtime-data`，未删除/重建数据库或 Volume。`.env`、`compose.yaml`、运行容器全部环境变量摘要保持一致；模型及安全核心源文件哈希与候选镜像核对通过。SSH 总开关仍为原来的 `false`，接入前须显式启用；本轮没有修改这一已有授权配置。low 推理、120 秒模型超时不变。

**数据核对中的真实差异：**开发开始与部署前快照之间，7 张表出现变化：业务画像版本 12→13、事实 176→192、图谱版本 14→15，上传记录变为 removed；新增版本时间为 2026-10-10 13:31:13 UTC，早于容器更新。这些差异的来源没有核实，不能声称整个开发期间主库从未变化。本轮执行记录中没有对主库调用上传删除、业务审核或模型调查写入 API，也未尝试回滚这些既有变动。

部署前快照与部署后快照 **42 张表全部相同**，无活动任务；**8 个资料文件哈希相同、6 次历史调查及历史模型超时诊断相同**。另外对原先的 12 版画像及 176 条事实做子集哈希校验，确认旧内容完整保留。证据：`before-data.json`、`pre-deployment-data.json`、`after-data.json`、`old-history-subsets.json`、`preservation.json`、`verification.json`，均在 `test-results/standard-ssh/`。

部署前差异检查失败后，执行顺序未停下来先分析差异就进行了容器更新，这是本轮操作顺序错误；随后已核对差异早于更新、部署前无活动任务、部署前后数据完全相同。没有宣称检测到了差异后数据仍在整个开发期间保持完全不变。

保留回退镜像 `atr-runtime-api:before-standard-ssh-20261010` 和 `atr-runtime-web:before-standard-ssh-20261010`；部署镜像选择保存在忽略的 `test-results/standard-ssh/deployment-compose.yaml`。回退只替换镜像，不重置数据。未提交、Push、Merge 或创建 PR。

## 11. 未解决风险与生产接入

客户端工具白名单只约束 ATR。真实测试已证明普通低权限 SSH 账号可由其他客户端在自己的 home 写文件；ATR 拒绝任意 Shell 不等于服务器阻止其他客户端使用 Shell。UID 和组检查不能证明没有特殊 sudoers、ACL、polkit 或可写启动文件。即使 AI 使用固定工具，授权日志/配置中的敏感数据仍须遵守现有脱敏及资料授权约定。

Docker 只读默认开是 ATR 的工具权限设置，不是 Docker API 的只读授权。普通 docker 组可带来高权限，因此本适配器拒绝该组；不能通过加入 docker 组或修改 socket 全局权限解决读取失败。需要服务器管理员另行验收已存在的安全读取入口、rootless 环境或受限访问方案。Docker daemon 的访问安全说明见 [Docker 官方文档](https://docs.docker.com/engine/security/protect-access/)。SSH 主机公钥和认证选项见 [Paramiko 官方文档](https://docs.paramiko.org/en/stable/api/client.html)。

随想记现有 `atr-reader` 锁定且 Shell 为 `/usr/sbin/nologin`，本轮没有连接、解锁、设置密码或修改它。后续接入须由管理员批准普通 SSH Shell 的账号配置变更和明确范围，提供可信主机公钥，核验权限后先运行基础工具，再审核服务映射。不能用 root 或提权来补齐权限。需要服务器端强制边界时，现有 `atr-read-v1` 网关继续可选，其部署由管理员另行执行。生产账号、真实 daemon、真实业务日志和实际模型调查均未在本轮验收。

## 12. 最终状态

```text
IMPLEMENTATION_STATUS=completed_local_deployed
EXECUTION_MODES=standard_ssh_readonly,forced_gateway
AUTH_MODES=password,private_key
STANDARD_SSH_CONNECTOR=implemented
FIXED_READONLY_TOOLS=13
ROOT_PROTECTION=username_uid_and_high_risk_groups
AGENT_AUTO_EXECUTION=authorized_readonly_tools_allowed
PASSWORD_SECURITY=memory_only_30_minutes
OPENSSH_ACCEPTANCE=67_passed_including_docker_mock_and_chrome
FULL_REGRESSION=378_passed_0_failed_0_skipped
DOCKER_HEALTH=api_healthy_web_http_200
PRODUCTION_SSH_CONNECTIONS=0
DATA_PRESERVED=true_deployment_snapshots_match_earlier_changes_documented
GIT_PUSHED=false
```
