# 随想记配置读取：实查报告与已批准部署清单

实查日期：2026-10-11。该清单已获操作者明确确认并实施，配置读取验收通过。下文保留批准时的变更前状态和执行计划供审计；最终结果见 `docs/suixiangji-config-read-acceptance-report.md`。

## 1. 已证实的拒绝原因

最近调查 Run：`908c62fe-46e9-4fce-84f8-459934d0dbe2`，绑定目标 `d997a5d5-8898-4f99-bbe7-765e38facac9`，状态 `unresolved`，停止原因 `permission_blocked`。

第五次工具调用 `a0c396df-2c02-4aed-a619-e133d6d12be6`：

```json
{"tool_id":"config.list","arguments":{"path":"/opt/suixiangji"}}
```

对应运行观测 `007935fd-b458-4c99-94da-9ec8bf1b7bfc`：`failure_stage=request`、`execution_mode=forced_gateway`、`error_code=tool_permission_denied`、`output_bytes=16`。没有输出敏感日志或原始模型内容。

客户端目标当前 `allowed_paths=["/opt/suixiangji"]`，请求通过本地路径检查。生产 `/etc/atr-read-v1/policy.json` 的 `allowed_paths=[]`。以真正的网关账号 `atr-read-v1` 执行相同协议请求，复现退出码 1、stderr `PermissionError`（16 bytes）。现有网关在打开文件之前先拒绝不在白名单中的路径。因此本次是**服务端路径白名单未开启**，不是模型错误，也不是本地 Scope 拒绝。

另外，两份真实 Compose 文件均为 root:root `0600`，网关账号不可直接读取。即便开放原目录，也不能解决全部文件访问；而直接开放整个目录还会使 `.env` 等秘密文件进入候选清单。本方案不修改这些原始文件权限。

## 2. 实际部署与配置位置

| 项目 | 只读核实的路径或状态 |
|---|---|
| API / DB Compose 项目 | `suixiangji-staging` |
| 工作目录 | `/opt/suixiangji/app/outputs/wealthmate_backend` |
| 基础 Compose | `/opt/suixiangji/app/outputs/wealthmate_backend/docker-compose.yml` |
| 覆盖 Compose | `/opt/suixiangji/staging-compose.yml`，使用合法 `!override` 标签 |
| API 配置定义 | `/opt/suixiangji/app/outputs/wealthmate_backend/app/config.py` |
| API Nginx / 下载站配置 | `/etc/nginx/sites-available/suixiangji-pre-cert.conf` |
| 对应启用链接 | `/etc/nginx/sites-enabled/suixiangji-pre-cert.conf`，符号链接，不直接授权 |
| 业务网站 Nginx | `/etc/nginx/conf.d/suixiangji-website.conf` |
| PostgreSQL 配置位置 | 容器内部 `/var/lib/postgresql/data/postgresql.conf`，不授权原始数据卷读取 |
| PostgreSQL 数据卷 | `suixiangji-staging_wealthmate_pgdata`，保持不变 |

已授权且保持不变的两个容器：

- API：`cf528bb67f9af69bfdf5e32a95af7d8e52c0ef571a1200c73aecf7d84b1e76fb`，名称 `suixiangji-staging-api-1`。
- DB：`2ee6513f72cb0ebd319b819d3f9b56b24329c5ea7f02c660a595853fe1800216`，名称 `suixiangji-staging-db-1`。

API 容器标签只记录基础 Compose；DB 标签记录基础和覆盖两份 Compose。覆盖文件声明绑定 `0.0.0.0`，当前 API 实际端口为 `127.0.0.1:18000 -> 8000/tcp`。磁盘配置声明与运行实例信息应分别作为线索，不能据此认定故障或修改业务部署。

## 3. 现有安全机制与限制

- 部署网关与本地 `scripts/ssh_gateway.py` 的完整文件 SHA256 相同：`6c39f1435d7ce209ed48ea59424cbbadcc5b694c3a7d359b99908471f24bc6d4`，无需更新程序。
- `scripts/ssh_gateway.py:233` 逐级 `openat/O_NOFOLLOW`，禁止路径穿越和符号链接；`:296` 开始检查配置路径，`:314` 限制为不超过 48,000 bytes 的普通文件。
- `config.list` 非递归、最多 100 个普通文件；**没有敏感文件名排除、分页或内容脱敏**。`config.read` 原样返回文件内容。
- `backend/app/connectors/policy.py:46` 开始本地白名单检查；`backend/app/connectors/ssh.py:154` 使用固定 SSH 命令 `atr-read-v1`，参数只通过 JSON stdin 传递；保留主机公钥固定验证、密码会话认证及 10 秒客户端超时。
- `backend/app/tools/redaction.py` 的正则脱敏并不覆盖任意格式的所有秘密，不能用它来证明原始 `.env` 安全。
- 网关没有文件系统挂载越界检查。因此只授权新建、root 管理、无子挂载的副本目录；禁止在该目录内添加 bind mount、符号链接、数据卷或原始秘密文件。只读探测确认 `/opt/suixiangji` 当前没有子挂载，祖先目录为 root 所有、`0755`。
- SSH 实际账号为 **atr-read-v1**（UID/GID 1001），不是 `atr-reader`。有效 SSHD 配置保留现有 Python ForceCommand、`DisableForwarding=yes`、`PermitTTY=no`、`PermitUserRC=no`；无需改 SSHD 或重启它。
- Docker `status/logs` 精确检查服务端容器白名单；`container.list` 当前只优先显示授权容器，并不是严格仅返回白名单。复查使用随想记名称过滤，未扩大原权限；这一现有清单限制不会在本轮改代码。

## 4. 已批准的准确变更（保留变更前对照）

| 对象 | 当前 | 拟修改后 |
|---|---|---|
| 生产策略 `allowed_paths` | `[]` | `["/opt/suixiangji/atr-reviewed-config"]` |
| ATR 已有目标 `d997a5d5…` 的 `allowed_paths` | `["/opt/suixiangji"]` | `["/opt/suixiangji/atr-reviewed-config"]` |
| 生产安全副本目录 | 不存在 | 新建 `/opt/suixiangji/atr-reviewed-config`，root:atr-read-v1 `0750` |
| 生产副本文件 | 不存在 | 下列 7 个普通 JSON 文件，root:atr-read-v1 `0640`，账号只读、无写权限 |
| 策略备份 | 不存在 | `/etc/atr-read-v1/policy.json.pre-config-read-20261011`，root:root `0600` |

**服务端策略其余字段全部保持原值**：`services=[]`、`docker_reads_enabled=true`、`containers` 仍为上述两个完整 ID。不增加 root 执行、不添加 Docker 组、不修改现有 ACL、不安装软件包。

只创建经审核的静态配置快照，不向网关开放原始业务配置、`.env`、数据库文件、证书私钥或任何其他业务目录。当前机器无 `setfacl`；由于不调整原始文件访问权，采用新建副本的专用组只读权限，不需要安装 ACL 工具或更改业务文件 ACL。

副本的本地源目录：`E:\ATR\test-results\suixiangji-reviewed-config`。采集时间 `2026-10-11T13:16:41.764382+00:00`。服务器目标为同名文件：

| 文件 | bytes | 审核后内容 |
|---|---:|---|
| `compose-base.reviewed.json` | 2041 | API/DB 镜像、依赖、端口、卷；环境变量仅名称，不含秘密值 |
| `compose-staging.reviewed.json` | 946 | 覆盖镜像、端口及 `!override` 说明 |
| `api-config.reviewed.json` | 2758 | 允许的字段类型和非敏感代码默认值；其余字段只列排除名称 |
| `api-runtime.reviewed.json` | 1045 | 明确白名单中的环境、demo/schema 开关、版本、构建号、JWT 有效分钟数及 Git SHA |
| `postgres-config.reviewed.json` | 1650 | 19 个固定非敏感磁盘配置参数及单位，没有密码、连接串或数据行 |
| `nginx-business.reviewed.json` | 5439 | 仅上述两个业务文件中的非敏感路由、端口、超时等选定指令；排除私钥相关指令 |
| `deployment.reviewed.json` | 2716 | 已授权两容器的名称、端口、挂载和部署标签，并明确声明与运行状态差异 |

合计 16,595 bytes；所有文件小于既有 48,000 bytes 限制。每份文件注明管理员快照、来源、采集时间及证据适用范围；源文件记录 SHA256。PostgreSQL 参数来自固定的只读 `postgres -C`，**并非实时 SQL SHOW，也不能证明数据库连接正常**。Nginx 内容来自磁盘文件，未运行 `nginx -T`，不能证明运行中的 Nginx 已加载这些内容。

本轮代码没有通用的目标路径更新接口。为保留同一 target_id 和人工映射，拟在本地已有 SQLite 中用单事务更新该目标唯一的 `config_json.allowed_paths` 字段，验证业务/目标身份、无活动任务，并保留其他所有字段。不修改业务画像、绑定、历史 Run、证据、预算或凭据引用。

`RuntimeService.get()` 每次从 SQLite 读取；网关每次连接读取策略。因此这两项配置不需要重建 API、重启 SSHD、重载 Nginx 或操作业务容器。API 不重启可保留尚未过期的内存密码会话。授权范围变化后应新建 Run；旧暂停 Run 的授权签名不一致会被现有校验拒绝，不能用恢复旧 Run 来绕过。

## 5. 准备好的执行与回滚命令

以下命令均从**本地 Windows PowerShell**执行。脚本已准备并提供全文供审阅，`check` 只读，`apply` 只有获得操作者确认后执行。

```powershell
Set-Location E:\ATR
python test-results/suixiangji-config-client-scope.py check
python test-results/suixiangji-config-server-deploy.py check

# 等待确认后才可执行：先备份和部署服务器副本，再同步客户端路径。
python test-results/suixiangji-config-server-deploy.py apply
if ($LASTEXITCODE -ne 0) { throw '服务器部署失败；不要继续更新客户端' }
python test-results/suixiangji-config-client-scope.py apply
if ($LASTEXITCODE -ne 0) { throw '客户端同步失败；按下列命令撤销服务器变更' }
```

服务器脚本使用现有 `quant-server` 管理 SSH 别名和已验证主机密钥，以 root 创建上述**配置副本及网关策略文件**。它不运行 Compose up/down、Nginx reload、业务命令或 SQL。先检查现有策略 SHA256 `353d077cafbf5d3d899494acfc05bb081c8170f2850d8cb1901a939463a5bc40`，拒绝已有目录/备份覆盖，校验原始配置源哈希，检查无挂载，再生成 root 专用备份。策略通过同目录临时文件 `policy.json.config-read-20261011.tmp` 原子替换；成功后该临时路径消失。

完整回滚（已实施两项变更时）：

```powershell
Set-Location E:\ATR
python test-results/suixiangji-config-server-rollback.py
if ($LASTEXITCODE -ne 0) { throw '服务器策略与预期不符，停止回滚并核对，避免覆盖其他管理员变更' }
python test-results/suixiangji-config-client-scope.py rollback
if ($LASTEXITCODE -ne 0) { throw '客户端路径与预期不符，停止并核对' }
```

服务器回滚只接受本次批准的新策略，验证备份哈希，将原字节以 root:atr-read-v1 `0640` 原子还原；暂存路径为 `policy.json.config-read-20261011.rollback.tmp`。副本目录改为 root:root `0700`，网关读权限撤销，**保留副本和备份而不清理任何生产文件**。本地回滚仅把同一目标的 `allowed_paths` 恢复为 `["/opt/suixiangji"]`，保留其他后来变更的字段。

若服务器部署通过、客户端同步未发生，只执行服务器回滚；客户端原路径无需回滚。若部署在策略生效前中断，原策略仍有效，可能留下尚未授权的副本/备份；先核对状态，不盲目再次部署或删除文件。

## 6. 验证结果

已完成的生产检查全部只读，通过管理 SSH + `runuser -u atr-read-v1` 使用真实部署网关的 JSON 协议：

| 检查 | 实际结果 |
|---|---|
| 原 `config.list /opt/suixiangji` | 拒绝，复现 `PermissionError`、16 bytes stderr |
| `config.read /etc/shadow` | 拒绝，退出码 1；未读取文件内容 |
| Docker list（随想记名称过滤） | 成功，两个授权 ID 均存在 |
| API / DB status | 两次成功 |
| API / DB logs，tail=1 | 两次成功；仅记录长度和成功状态，未输出原始日志 |
| 源配置、有效 SSHD、祖先权限及挂载核对 | 完成；ForceCommand / DisableForwarding 保持有效 |
| 服务器部署脚本 check | 通过，server_written=false |
| 客户端路径变更脚本 check | 通过，database_written=false；仅拟更改 allowed_paths |
| 新目录 list/read | 批准后完成部署，真实网关读取全部 7 份副本通过；ATR 客户端清单及 4 份代表性配置读取通过 |

本地保存保护基线：SQLite 42 张表、405 个资料文件、`quick_check=ok`、无 queued/running 任务。部署后将比较所有表和资料哈希；允许的业务配置变化仅为指定 `runtime_targets` 的一行。必要的手动采集测试会新增正常工具观测/任务审计，不修改人工确认记录或历史证据。

批准后的上述验收已完成：本地相关回归 49 项通过，隔离 Linux 安全检查 18 项通过，生产网关协议检查 19 项通过，ATR 实际客户端工具观测 6 项通过（5 次成功、1 次本地拒绝）。未创建生产调查 Run、调用付费模型或注入故障。完整审计编号、数据比较和当前会话期限见最终报告。

## 7. 下一次真实 Run

仅在本报告更新为“配置读取验收通过”后：

1. 在业务 `suixiangji` 中选择目标 `suixiangji安全网关2`（`d997a5d5-8898-4f99-bbe7-765e38facac9`），执行模式 `forced_gateway`。
2. 页面确认授权目录为 `/opt/suixiangji/atr-reviewed-config`，不要再填写整个 `/opt/suixiangji` 或原始 `.env`。
3. 检查既有 API、DB 两个人工映射仍为 confirmed 且有效，完整容器 ID 对应本报告两个容器；不得自动改映射。
4. 原会话已过期，操作者重新在页面提供密码后，当前会话到期时间为 `2026-10-11T13:55:17.639298+00:00`（北京时间 21:55:17）。过期后为用户名 `atr-read-v1` 重新提供密码；不自动续期或落盘。
5. 手动创建**新**的只读调查，确认预算快照 `max_input_tokens=20000`；累计预算、模型/工具次数、输出限制及安全策略保持现有值。
6. 实际调查应分别引用实时 Docker 证据和带时间戳的配置快照。没有证实故障时输出“无已证实故障”，不能由配置差异编造根因或执行自动修复。

本轮不会启动上述 Run。受控故障注入需另外确认，当前不执行。

## 8. 证据文件及代码位置

- `test-results/suixiangji-config-read-probe.json`：真实服务端策略、身份、有效 SSHD、部署路径。
- `test-results/suixiangji-config-files-probe.json`：文件元数据、原始 Compose 不可读与网关拒绝复现。
- `test-results/suixiangji-config-safe-review.json`：筛选后的非敏感配置、运行参数及 19 个 DB 磁盘参数。
- `test-results/suixiangji-reviewed-config-manifest.json`：7 份已审核副本的字节数和完整 SHA256。
- `test-results/suixiangji-gateway-before-regression.json`：真实 Docker / 拒绝测试和无子挂载证据。
- `test-results/suixiangji-config-local-readiness.json`：实际 API 的目标就绪状态、路径及 20,000 输入预算。
- `test-results/suixiangji-config-preservation-before.json`：只读数据库/资料保护基线，不含原始行数据。
- 相关实现：`scripts/ssh_gateway.py:233`、`backend/app/connectors/policy.py:57`、`backend/app/connectors/ssh.py:147`、`backend/app/connectors/service.py:116`、`backend/app/agent/adaptive.py:98`、`backend/app/agent/business_investigation.py:92`、`backend/app/system/scope.py:367`、`backend/app/connectors/credentials.py:12`。

仍存在的限制：静态配置快照需管理员在部署变化后审核刷新；无法读取秘密相关字段、原始 DB 数据卷或其他业务；网关本身不拦挂载越界、不可靠识别任意秘密；现有 container.list 不是精确白名单过滤。上述限制明确保留，不扩大权限、不重构 Agent、不 Push/Merge/PR。
