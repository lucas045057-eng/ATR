# ATR V2.2 SSH 用户名密码认证兼容改造

## 本轮范围与结论

本轮在 `E:\ATR` 复用现有 Paramiko 连接器及 `atr-read-v1` 网关，增加用户名密码认证，保留私钥认证。未实现普通 Shell 兼容模式。

密码通过专用认证接口提交，服务器配置只存认证方式与随机凭据引用。明文密码只在后端进程内存存在，不进入普通服务器配置、业务画像、数据库、日志、审计或模型上下文。浏览器输入框在提交时清空，应用不写入 localStorage/sessionStorage，页面及 API 不回显密码。

密码会话固定有效期 **30 分钟**；后台每 30 秒清除过期条目，每次取用立即检查有效期。每条凭据绑定操作者、目标 ID、地址、端口、用户名和主机公钥。最多保存 100 条；刷新会替换旧引用，可主动清除。连接后、执行前、读取期间和返回结果前再次检查，过期或清除后不执行下一项工具、不生成成功观测。认证失败或主机公钥不匹配会清除密码会话，发现任务立即停止本次采集，不自动尝试后续工具。

后端启动与关闭均清空凭据，容器重启后必须重新输入。当前 Docker 部署为单 API 进程，异步调查线程共享进程内凭据库。多 API 进程或横向扩容不能共享此凭据库，本轮没有实现无人值守持久凭据。

Python 的不可变字符串及第三方认证库不能承诺内存字节的可靠覆盖擦除；本轮保证停止引用、及时失效与不主动落盘，不宣称可抵抗宿主机管理员的内存读取。需由部署环境控制内存转储和宿主机访问权限。

## 审计结果与兼容方式

开始时工作区已有模型隔离、结构化输出、错误治理与调查证据相关未提交改动，均予保留；本轮没有重置工作树或覆盖这些功能。

原 SSH 客户端已禁用 agent 和本地私钥查找，使用 RejectPolicy 与已确认的完整主机公钥，固定执行 `atr-read-v1`，通过标准输入传递受校验 JSON。原运行环境创建、工具策略、服务映射人工确认、服务端策略文件是已有权限层。

新增 `auth_mode`：`private_key` / `password`。缺少该字段的旧配置仍解释为 `private_key`，继续通过原 `SSH_CREDENTIALS` 引用到私钥文件。不新增数据库表或迁移，不要求 password 模式填写私钥引用。

通用 StrictModel 会去除字符串首尾空白，不适用于密码。专用密码请求模型关闭该规则、保留密码原值，使用 SecretStr；格式错误只返回固定安全提示，不回显提交值或不可信字段名。

Paramiko 默认密码认证可能回退到 keyboard-interactive。密码模式使用现有 SSHClient 的认证策略入口，显式调用 `auth_password(..., fallback=False)`，仍设置 `allow_agent=False`、`look_for_keys=False`。没有手写 SSH 协议或增加新的 SSH 客户端依赖。

连接、认证、banner、通道及读取均有界超时。root 在请求校验及实际连接前均被拒绝，包括旧配置绕过新模型校验的情形。安全错误区分凭据失效、认证失败及主机公钥不匹配；审计继续仅保存异常类型。

模型 worker、low 推理配置、120 秒模型超时、模型及 Base URL/Key、工具白名单、证据门、store 授权、服务映射审核、默认关闭的 Docker 读取与零自动重试规则均保留。

## 页面及 API

「服务器范围」可选择用户名 + 私钥或用户名 + 密码。密码方式只显示密码输入框，保存环境后单独提交密码。已有密码目标显示有效期，提供更新、清除按钮；没有可用会话时禁用轻量发现。页面每 15 秒更新凭据状态，API 在每次操作时立即检查，页面显示延迟不扩大权限。

受原操作者 Bearer 认证保护的接口：

- `POST /api/v1/businesses/{business_id}/targets`：普通目标配置，仅包含 `auth_mode`、空或合法的私钥引用等非密码字段。
- `POST /api/v1/businesses/{business_id}/targets/{target_id}/credential`：专用敏感请求体中的 `password`，返回目标的非敏感配置及会话状态。
- `DELETE /api/v1/businesses/{business_id}/targets/{target_id}/credential`：清除内存会话。
- 原 targets 列表返回 `credential_status.state` / `expires_at`。旧目标默认私钥方式。

密码不能作为普通配置中的附加字段，不从 URL、查询参数或配置文件获取。客户端不能通过复制引用给另一个目标获得密码使用权。清除后的数据库中可以保留失效的随机引用，其本身不包含密码，也不能绕过后端的操作者和目标绑定。

## 验收方法及证据

仅使用独立 `atr-ssh-password-test` Docker 项目及本地临时测试数据库，未连接随想记或任何生产服务器。隔离服务包含 Linux/OpenSSH、候选 API 与候选前端。密码账号 UID 10003，原私钥账号 UID 10002；均无 root、sudo 或 Docker socket 权限。

OpenSSH 密码接口启用 `Match User atrpassword` + `AuthenticationMethods password` + `ForceCommand`，禁用公钥认证，`AuthorizedKeysFile none`。因此密码成功不依赖向该账号添加公钥。已有私钥测试接口保持原认证及网关逻辑。

测试密码随机生成在验收进程内存，通过二进制标准输入送给隔离容器计算哈希；只保存合成密码哈希以建立测试账号，不保存该明文到环境变量、命令行或文件。合成服务器的身份密钥文件只用于隔离测试。

测试容器关闭 OpenSSH 10 的 PerSourcePenalties，避免集中运行错误密码/主机校验/拒绝操作用例时触发临时来源封禁。这是测试环境的速率条件，不是正式 ATR 行为；生产不应因此关闭防护。固定网关、文件权限、工具策略和证据门始终启用。

Windows stdin 换行最初改变了测试哈希输入，已改为二进制传输。验收配置最初漏了默认字段，已改用现有 RuntimeTargetCreate 生成。初轮浏览器回归发现密码输入标签与提示文字合并影响定位，已添加明确的无障碍标签。上述失败记录保留，最终通过记录与早期失败分开。

已通过的隔离验收：**34 项**，0 失败、0 跳过（其中原私钥 OpenSSH 18 项、密码 OpenSSH 14 项、Docker/Chrome 页面 2 项），另执行 UID 10002 / 10003 的真实文件权限检查，均拒绝对策略、网关和授权文件写入。

| 要求 | 验证结果 |
|---|---|
| 用户名密码成功 | 真实 Paramiko → OpenSSH → 固定网关，host.info/config.read 成功；密码接口不启用公钥 |
| 错误密码 | AuthenticationException，固定安全信息，清除会话；停止发现后续认证 |
| 主机公钥不匹配 | BadHostKeyException，拒绝连接，不能自动接受 |
| root 拒绝 | 配置校验、连接前与 OpenSSH AllowUsers/PermitRootLogin 三层拒绝 |
| 写命令、sudo、未授权 store 服务 | 服务端网关拒绝，即使恶意客户端扩大自身 allowed_paths 也不生效 |
| SFTP、SCP、PTY、普通 Shell、转发 | 原始恶意 SSH 请求实际被拒绝；客户端未开放对应工具 |
| 过期与清除 | 短 TTL 的真实连接失效、排队后过期、连接后执行前过期、并发刷新后旧引用清除均有检查 |
| 明文凭据泄露 | 实际 Docker API 数据库、日志、环境及验收文件扫描无随机密码；模型 Prompt/上下文另用本地模型 Stub 捕获验证 |
| 异步及重启 | 实际 Docker 异步采集成功；重启后状态 required、工具入口拒绝，重新输入后成功 |
| 私钥兼容 | 原 18 项 OpenSSH 回归及原本地协议测试通过 |

证据目录：`test-results/ssh-password/acceptance-b91fb6dd/`，包含 `summary.json`、`junit.xml`、`tests.log`、页面截图、合成测试配置；凭据明文未写入这些文件。测试服务已停止，数据和证据保留，未删除 Volume。1 条 PytestAssertRewriteWarning 来自脚本预加载测试模块以注入内存合成口令；断言仍执行，测试没有跳过。

冻结版本完整回归 **325 项全部通过，0 失败、0 跳过**，用时 564.90 秒；证据为 `test-results/ssh-password/full-frozen.xml` / `full-frozen.log` / `full-frozen.exit`（退出码 0）。其中包含原模型隔离、错误治理、证据门、授权边界及浏览器回归。

定向回归 **34 项通过**（20 项新增凭据检查及原 SSH 协议、运行范围相关检查）；此前完整回归 **324 项通过**。全仓 Ruff、前端 TypeScript/构建和 Git 空白检查已通过。

## 本地运行状态与数据保留

已将本地 API、页面更新为 `atr-ssh-password-api:20261010` / `atr-ssh-password-web:20261010`，通过忽略的 `test-results/ssh-password/deployment-compose.yaml` 指定镜像，没有改动基础 compose 或 `.env`。

本地 API `127.0.0.1:28000` 和页面 `127.0.0.1:28080` 均返回 200，API healthy。现有 SSH 开关保持 `false`；模型推理仍是 `low`、超时仍是 120 秒。正式连接需按下文显式启用，并先由服务器所有者配置强制网关。

`preservation.json` 核验：原数据库 **42 张表**的行数与完整内容摘要一致，**8 个资料文件**摘要一致，**6 次历史调查**保留；原 `atr-runtime_runtime-data` Volume 沿用。模型/工具策略源文件、`.env`、基础 compose 的摘要及运行配置相同。

回滚镜像已保留：`atr-runtime-api:before-ssh-password-20261010`、`atr-runtime-web:before-ssh-password-20261010`。回滚时只切换镜像并沿用原数据挂载，不删除数据或 Volume。

本轮真实付费模型调用 **0 次**，生产 SSH 连接 **0 次**；未 push、merge 或创建 PR。

## 本轮修改文件

- `backend/app/connectors/credentials.py`：内存凭据库、作用域绑定、过期、清除与安全错误。
- `backend/app/connectors/models.py`：认证模式、专用 SecretStr 请求、root 拒绝。
- `backend/app/connectors/ssh.py`：显式密码认证、失效检查、失败清除，保留旧私钥路径与固定网关。
- `backend/app/connectors/service.py`：会话提交/状态、普通配置拒绝密码引用、安全错误信息。
- `backend/app/api/business.py`：认证接口、敏感验证错误固定提示、排队前检查。
- `backend/app/main.py`：启动/关闭清除，定期过期清理。
- `backend/app/system/scope.py`：遇终止性 SSH 认证错误停止发现，避免连续认证尝试。
- `frontend/src/business.tsx`：模式选择、独立提交、更新/清除和失效提示。
- `tests/test_ssh_password.py`、`tests/e2e/test_ssh_password_ui.py`：凭据、异步、诊断、模型上下文及页面回归。
- `tests/e2e/test_browser.py`：专用 SSH 页面验收启用开关，保留默认 Mock 验收。
- `tests/integration/ssh_password_acceptance.py`：真实隔离 OpenSSH 密码、网关、SFTP/PTY/SCP/转发与失效验收。
- `tests/fixtures/linux-ssh/{Dockerfile,start.py,check_permissions.py}`：扩展已有夹具的低权限密码账号与 SSHD Match 网关，保留原私钥测试。
- `scripts/verify_ssh_password.py`：可复跑的独立 Docker 验收，保留测试数据并只停止自身服务。
- 本报告。

本轮没有修改 `settings.py`、`.env`、`compose.yaml`、模型模块、工具策略或服务端网关实现。它们可能显示为未提交，属于之前任务的改动，不能误认为本轮新变更。

## 下一步生产接入（本轮未执行）

1. 由服务器所有者选定现有低权限账号，确认 UID 非 0、无 sudo/特权组/Docker socket 访问；不要使用 root，也不需要 ATR 自动创建生产账号。
2. 由所有者安装现有 `scripts/ssh_gateway.py` 为 root 所有、账号不可修改的固定入口，授权策略文件和相关目录同样不可由账号修改；只列出已授权服务和业务目录，不扩张 store 权限。仅有密码不能建立服务器端只读边界。
3. 由所有者审核 SSHD 的账号级 Match 配置，并保留现有私钥认证。例如，下面用户名需换成该已有账号，路径需换成审核后的固定绝对路径：

```sshdconfig
Match User atr-read
    PasswordAuthentication yes
    PubkeyAuthentication yes
    KbdInteractiveAuthentication no
    PermitEmptyPasswords no
    AuthenticationMethods password publickey
    ForceCommand /usr/bin/python3 /opt/atr/ssh_gateway.py --policy /etc/atr/policy.json
    DisableForwarding yes
    PermitTTY no
    X11Forwarding no
```

`AuthenticationMethods` 的两个空格分隔方案表示 password 或 publickey 任一种，而非必须两者同时使用。密码不依赖 authorized_keys；保留公钥选项用于已有私钥方式。SSHD 的原有环境变量接收与可执行路径也需审核，避免用户可修改的环境或入口影响网关。

4. 所有者先执行 `sshd -t` 及账号条件下的 `sshd -T -C user=...,host=...,addr=...` 核对最终规则，再安全重载；具体方式按该服务器 OpenSSH/系统版本确认。ATR 本轮没有修改或重载生产 SSHD。
5. 通过可信的独立渠道核对完整 SSH 主机公钥，再在本地 ATR 显式启用 `SSH_ENABLED`，在页面选择 password、填写账号与密码、确认服务器端强制网关。不要通过命令行参数或 URL 传递密码；远程访问 ATR 时使用可信 HTTPS。
6. 首次只做基础只读采集，核对账号实际可读范围，人工确认服务映射和业务画像后才开始调查。主机公钥或认证失败时停止并核验，不自动接受新主机钥匙，不使用 root/sudo 绕过权限。
7. 过期、主动清除、后端重启后重新输入。无人值守需求另行设计独立密钥管理和加密凭据服务，不将密码写入 `.env` 或数据库。

配置语义依据：[OpenSSH sshd_config 官方手册](https://man.openbsd.org/sshd_config)、[Paramiko SSHClient 官方文档](https://docs.paramiko.org/en/stable/api/client.html)、[Paramiko Transport 官方文档](https://docs.paramiko.org/en/stable/api/transport.html)。
