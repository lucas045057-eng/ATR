# ATR V2.2 随想记配置读取部署与验收报告

日期：2026-10-11。结论：**经操作者确认，已完成现有网关权限配置；真实 ATR 客户端 config.list/config.read 通过。未创建生产调查 Run，未调用模型，未执行故障注入。**

## 1. 实际根因

Run `908c62fe-46e9-4fce-84f8-459934d0dbe2` 的第五次工具调用请求为 `config.list {"path":"/opt/suixiangji"}`。客户端已授权该目录，审计显示请求阶段拒绝，远端错误输出 16 bytes。

生产网关 `/etc/atr-read-v1/policy.json` 的 `allowed_paths=[]`，以实际账号 `atr-read-v1` 使用相同协议复现 `PermissionError`。**本次阻断来自服务端路径白名单，发生在打开目录前。** 两份原始 Compose 文件另外存在 root:root `0600` 文件权限限制。没有用 chmod 777、root 工具权限或开放原目录绕过这些限制。

## 2. 实际业务路径与本次开放范围

实查确认基础 Compose 和 API 配置位于：

- `/opt/suixiangji/app/outputs/wealthmate_backend/docker-compose.yml`
- `/opt/suixiangji/app/outputs/wealthmate_backend/app/config.py`
- 覆盖 Compose：`/opt/suixiangji/staging-compose.yml`
- 业务 Nginx：`/etc/nginx/sites-available/suixiangji-pre-cert.conf` 和 `/etc/nginx/conf.d/suixiangji-website.conf`
- PostgreSQL 磁盘配置：DB 容器内部 `/var/lib/postgresql/data/postgresql.conf`，原始数据卷没有加入读取范围。

现有网关不排除敏感文件，也不在服务端脱敏，因此授权管理员审核后的独立目录 `/opt/suixiangji/atr-reviewed-config`，没有开放原始配置树、`.env`、证书私钥、数据库文件或其他业务。

其中 7 份文件共 16,595 bytes：`compose-base.reviewed.json`、`compose-staging.reviewed.json`、`api-config.reviewed.json`、`api-runtime.reviewed.json`、`postgres-config.reviewed.json`、`nginx-business.reviewed.json`、`deployment.reviewed.json`。

文件包含已审核的依赖、镜像、端口、挂载、非敏感 API 运行开关/版本、19 个固定 PostgreSQL 磁盘参数、业务 Nginx 选定指令和部署元数据。环境秘密值、数据库连接串、密钥内容、原始 `.env` 均未进入副本。每份文件明确标注管理员静态快照、来源、采集时间及适用范围，源文件带 SHA256；不把磁盘声明当成持续实时状态。

## 3. 已实施变更

| 对象 | 修改前 | 修改后 |
|---|---|---|
| 服务端 `policy.json.allowed_paths` | `[]` | `["/opt/suixiangji/atr-reviewed-config"]` |
| 本地目标 `d997a5d5-8898-4f99-bbe7-765e38facac9` 的 `allowed_paths` | `["/opt/suixiangji"]` | 同一副本目录 |
| 副本目录 | 不存在 | root:atr-read-v1 `0750` |
| 7 份副本 | 不存在 | root:atr-read-v1 `0640`，无账号写权限 |
| 网关策略备份 | 不存在 | `/etc/atr-read-v1/policy.json.pre-config-read-20261011`，root:root `0600` |

策略备份完整哈希为 `353d077cafbf5d3d899494acfc05bb081c8170f2850d8cb1901a939463a5bc40`，已验证。网关 Python 文件未改动，现有 SSH ForceCommand、主机公钥校验和密码会话机制保持有效。

`services=[]`、`docker_reads_enabled=true` 和两个容器白名单完整保留：

- API `suixiangji-staging-api-1`：`cf528bb67f9af69bfdf5e32a95af7d8e52c0ef571a1200c73aecf7d84b1e76fb`。
- DB `suixiangji-staging-db-1`：`2ee6513f72cb0ebd319b819d3f9b56b24329c5ea7f02c660a595853fe1800216`。

本地客户端脚本在单一 SQLite 事务内只修改指定目标的 `allowed_paths`，验证目标身份、空闲状态和其他字段不变。随后操作者在页面正常重新提交过期密码，新会话引用由既有 API 更新；密码没有写入数据库。没有新建目标、改人工映射、修改业务画像或历史 Run。

没有调整原始配置文件 ACL 或权限，没有安装软件包，没有重启 API、SSHD、Nginx 或业务容器。授权路径从数据库按请求读取、服务端策略按 SSH 连接读取，已立即生效。

## 4. 验收结果

| 验证 | 结果 |
|---|---|
| 本地相关网关、SSH、密码会话等回归 | **49 passed** |
| 无网络的隔离 Linux 安全测试 | **18 项通过**，实际读取 UID 1001，覆盖 7 份副本、客户端越界、符号链接、路径穿越、FIFO、超大文件、非法工具/容器和无写权 |
| 生产真实网关协议 | **19 项通过**：目录 list、7 份 read、6 次拒绝、5 次 Docker 读取 |
| 生产拒绝测试 | 原 `.env`、整个原业务根目录、`/etc/shadow`、Docker socket、`..` 路径和未授权容器均被拒绝 |
| ATR API → 密码 SSH → ForceCommand | **6 项通过**：1 次 list、4 次 read 成功；原 `.env` 在本地 `parameters` 阶段拒绝，未发送 SSH 请求 |
| 读取内容一致性 | 7 份服务器文件全部匹配审核哈希；4 份经 ATR API 读取的文本哈希也一致 |
| 副本访问边界 | 普通文件、单链接、无符号链接、无子挂载，目录/文件均不可由网关账号写入 |
| Docker list/status/logs | 授权两容器读取正常，启动时间及重启计数均与部署前一致 |
| API/DB 人工映射 | 仍为 confirmed/current，同一目标，记录哈希未变 |
| 本地 ATR API | healthy，`/api/v1/health` 返回 200，仍使用 `atr-runtime_runtime-data` |

生产协议测试以实际网关账号执行真实部署程序；ATR API 测试另行验证了真正的密码 SSH 认证、固定主机公钥及 SSHD ForceCommand。没有只凭管理账号的本地读取宣称客户端通过。

ATR API 读取了 `compose-base`、`api-runtime`、`postgres-config`、`nginx-business` 四份副本。完整 job_id、observation_id、目标和错误阶段位于 `test-results/suixiangji-config-api-verification.json`；读取观测合计 12,335 bytes，没有原始敏感日志。

最初测试环境缺少容器 pytest，Windows 默认临时目录也不可访问；改用已有本地依赖和新的项目内测试目录后，49 项全部通过。隔离 Linux 测试仅在一次性容器中用 root 准备固定权限夹具，随后降为 UID 1001 验证，生产 ATR 仍使用原低权限账号。

## 5. 数据、预算与安全保留证明

比较部署前后 SQLite 42 张表和 405 个资料文件：

- `runtime_targets` 数量保持 4，仅现有目标路径配置及操作者正常刷新会话引用发生变化。
- 必要手动只读验收新增 6 个 `runtime_read` 完成任务、6 条工具观测：jobs 20→26、runtime_observations 67→73；没有调查任务或 Run。
- **其他 39 张表哈希全部不变**，包括人工映射、业务画像、图快照、历史调查、证据、模型请求及预算。
- 405 个资料文件整体哈希不变，SQLite `quick_check=ok`，无未完成后台任务。
- 本地数据 Volume 保留；生产两容器状态、启动时间、重启计数和既有 DB Volume 保持不变，没有业务容器写操作。

实际配置保持：单次输入 20,000，累计 Token 100,000，模型调用 8，工具调用 20，单次模型输出 1,500，推理级别 low；SSH 超时 10 秒、输出上限 48,000 bytes。现有目标累计读取 33/100，下一轮仍有余量。证据门、严格模型引用、工具白名单、零自动重试和 Run 目标绑定均未修改。

## 6. 回滚命令

从本地 PowerShell 执行已准备、全文可审阅的脚本：

```powershell
Set-Location E:\ATR
python test-results/suixiangji-config-server-rollback.py
if ($LASTEXITCODE -ne 0) { throw '停止：服务器策略已变化，先核对以免覆盖其他变更' }
python test-results/suixiangji-config-client-scope.py rollback
if ($LASTEXITCODE -ne 0) { throw '停止：客户端路径已变化，先核对' }
```

服务端脚本验证当前新策略及原备份哈希，原子恢复 `/etc/atr-read-v1/policy.json` 的原字节与 root:atr-read-v1 `0640`。副本目录改为 root:root `0700`，撤销网关访问，保留副本和备份，不清理生产文件。客户端仅恢复同一目标的 `allowed_paths=["/opt/suixiangji"]`，保留新凭据引用及其他字段。没有生产回滚演练或业务服务重启。

## 7. 下一次真实 Run 操作

1. 打开 ATR → `suixiangji`，选择 **suixiangji安全网关2**，核对目标 ID `d997a5d5-8898-4f99-bbe7-765e38facac9`、执行模式 `forced_gateway`。
2. 核对授权路径为 `/opt/suixiangji/atr-reviewed-config`，Docker 读取开启；API/DB 仍为已确认、有效的映射。
3. 目前密码会话 ready，到期时间北京时间 **2026-10-11 21:55:17**。过期后在页面为 `atr-read-v1` 重新输入密码，不在聊天、配置文件或存储中保存密码。
4. 手动新建只读调查，核对预算快照 `max_input_tokens=20000`，不要恢复授权范围变更之前的旧暂停 Run。
5. 检查审计仅使用上述 target_id，配置读取路径仅来自副本目录；证据应区分实时 Docker 状态和管理员配置快照。
6. 若没有足够证据确认实际故障，应输出“无已证实故障”，保留未解问题，不能编造根因或自动修复。

后续受控故障注入应另行批准，选择隔离验收对象，预先约定影响、时间窗和恢复步骤。本轮没有向当前生产环境注入故障。

## 8. 剩余限制与交付文件

副本采集时间为 `2026-10-11T13:16:41.764382+00:00`，**业务部署变化后需要管理员审核刷新**。PostgreSQL 使用固定参数 `postgres -C` 读取磁盘设置，没有执行 SQL SHOW 或证明数据库连接正常；Nginx 副本不是运行配置加载证明。

当前磁盘覆盖 Compose 声明 `0.0.0.0`，当前 API 实际绑定 `127.0.0.1:18000`；API 和 DB 的 Compose 标签文件列表也不同。这些事实作为待核查线索保留，本轮未认定其为故障、未改配置。

网关没有任意秘密的可靠过滤，也没有挂载边界自动校验，因此不能自行向副本目录放原始文件、建立链接或挂载。当前目录 root 管理、无上述对象；模型和网关账号不能新增文件。现有 Docker list 只是优先/过滤展示，不能宣称具备严格的服务端容器清单白名单过滤；status/logs 仍严格限制两个 ID。

本轮交付两个中文文档：本报告及 `docs/suixiangji-config-read-deployment-plan.md`。没有修改 ATR 应用代码；保留之前已存在的未提交改动，没有 Push/Merge/PR。

可核验的本地证据：

- `test-results/suixiangji-server-config-apply.json`、`suixiangji-client-scope-apply.json`：实际变更。
- `test-results/suixiangji-reviewed-config-manifest.json`：7 份副本精确大小、哈希。
- `test-results/suixiangji-server-config-verification.json`：生产 19 项协议验证、拒绝阶段和容器保留状态。
- `test-results/suixiangji-config-api-verification.json`：真实 ATR 客户端 6 项工具观测。
- `test-results/suixiangji-config-regression-tests.log`、`suixiangji-config-linux-verification.json`：49 项回归和 18 项隔离验证。
- `test-results/suixiangji-config-preservation-before.json`、`suixiangji-config-preservation-after.json`：42 表与资料哈希比较。
- `test-results/suixiangji-config-final-settings.json`：预算、绑定有效性、读取余量、API 健康与数据挂载。

```ini
CONFIG_READ_STATUS=passed
SSH_TARGET_ID=d997a5d5-8898-4f99-bbe7-765e38facac9
AUTHORIZED_CONFIG_ROOT=/opt/suixiangji/atr-reviewed-config
DOCKER_READS_PRESERVED=true
BUSINESS_BINDINGS_PRESERVED=true
HISTORICAL_DATA_PRESERVED=true
PRODUCTION_RUNS_CREATED=0
REAL_MODEL_CALLS=0
FAULTS_INJECTED=0
GIT_PUSHED=false
```
