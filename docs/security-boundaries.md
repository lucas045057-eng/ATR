# 安全与只读边界

## 上传与本地解析

所有源码、配置、日志、模型输出均视为不可信资料。解包逐项验证，拒绝绝对路径、路径穿越、非法/保留文件名、大小写重复路径、ZIP 链接/加密成员、TAR 符号链接/硬链接/设备，禁止递归展开嵌套压缩包。输出路径始终在受控资料目录。魔数与文本检查防止压缩包/二进制伪装源码。

| 限额 | 默认 | 配置 |
|---|---|---|
| 单次压缩包/单文件上传 | 50 MiB | UPLOAD_MAX_BYTES |
| 展开总量及当前业务有效资料总量 | 200 MiB | EXTRACTION_MAX_BYTES |
| 成员数量 | 2,000 | ARTIFACT_MAX_FILES |
| 单个展开文件 | 20 MiB | ARTIFACT_MAX_FILE_BYTES |
| 单文件解析读取 | 256 KiB | ARTIFACT_READ_BYTES |
| 路径深度 | 16 | ARTIFACT_MAX_DEPTH |
| 解包期限 | 45 秒 | EXTRACTION_TIMEOUT_SECONDS |
| 压缩比 | 200 | 固定拒绝阈值 |
| 解析 worker 内存 | 768 MiB | 固定上限 |

上传原始字节流边写边检查大小，任务锁与队列限制并发。解析在可信 ATR 子进程内，Linux RLIMIT_AS/CPU，Windows JobObject 内存/单进程限制，父进程强制期限（解包期限 + 10 秒启动余量）。Windows 无法设限即停止。它不是容器或完整操作系统沙箱；保护主要来自只使用安全数据解析器、资源上限与从不执行资料。

Python AST 仅解析，不 import 资料；JS/TS 仅识别元数据；YAML 拒绝锚/别名及过大 token 流，safe_load。不会执行上传脚本、构建、安装、钩子、Compose。旧本地仓库入口仅允许已配置根目录，忽略目录进入前剪枝，额外限制深度/条目/耗时，不自动克隆 URL。

失败资料留在本地隔离目录，已有业务知识不清空。删除/替换资料为逻辑停用，历史和失败文件仍占磁盘；目前无自动清理策略。操作者须保护 ARTIFACT_ROOT，不让其他本地账号篡改内容，使用操作系统磁盘权限/加密。应用不提供原始资料下载接口；局部读取再次验证 SHA256 与路径。

## 模型与敏感信息

默认 EXTERNAL_MODEL_ENABLED=false。首次只发送受限结构化摘要和元数据，故障时可发送脱敏局部源码/短日志；原包、完整源码、完整配置、全部历史不会默认发送。SecretStr 管理模型密钥和令牌，业务画像不存 SSH 私钥，校验错误也不回显提交的私钥文本。

后端/Collector 递归遮盖常见秘密键、Authorization、Bearer/Basic、带凭据 URL、JWT、常见 AWS 密钥、引号内含空格的值。原始上传本地文件不改写。模式脱敏不能识别所有业务自定义秘密；启用外部模型前需要操作者判断资料和供应商是否允许使用。

提示词只把资料当证据。模型输出必须 JSON，画像推断限定已存在 subject/object、谓词白名单及已选文件引用；模型不能注册工具、创建任意命令或自动确认事实。调查动作由服务器候选表产生，模型只作选择，Policy 独立审核。恶意提示文本/额外工具字段回归验证不会产生远程动作；这不保证所有语义推断不受提示污染，人工审核仍必要。

## 客户端固定工具

支持 host.info/memory/disk、process.list、network.listeners、service.list/status/logs、container.list/status/logs、config.list/read。未知工具/额外参数拒绝；服务名、容器 ID、tail/limit 等有 Pydantic 范围限制。config 路径须在显式业务路径之下，拒绝 /、/etc、/var、/home、/root、/usr、/proc、/sys、/dev 等宽泛根目录及穿越。

service/container 深读须有当前 confirmed 绑定；候选匹配排名不授权。config.read 的授权来自用户具体 allowed_paths 与服务端策略，不需要伪造服务绑定。config.list 单层，不递归。运行目标有累计 max_calls（失败/拒绝也留下记录）、超时及输出总字节上限；调查另受预算工具限制。

审计先于执行，记录目标、工具、脱敏参数、状态、观测时间、数据量、摘要及错误类型。拒绝/权限不足/断开/超时均可追溯，失败输出不会生成有效实时证据。远端时钟不被盲目信任，Runtime 使用本地采集时间。

## SSH 双层约束

客户端 Paramiko 使用固定主机公钥和 RejectPolicy，禁用自动接收未知密钥、SSH agent 和本机其他默认密钥。exec 请求始终是 `atr-read-v1`，参数经 stdin JSON 传入，无 shell 拼接。无 PTY、自由命令、scp/sftp 或 sudo 功能。SSH_ENABLED、access_authorized、credential_ref、已确认公钥和 forced_command_gateway 权限声明均须满足。

客户端白名单不能让一个有写权限的账号变成只读。生产级边界取决于服务器管理员配置并复核：专用无特权账号不能写业务文件/配置/数据库、不能控制服务，网关/策略/SSH 强制入口由管理员持有，账号不可改写；禁用密码/其他认证入口、交互终端、转发、用户自定义 rc 和绕过强制命令的方式。不为读取日志增加 sudo/root 或宿主机 Docker socket 权限。

`scripts/ssh_gateway.py` 为可选 Linux 标准库实现，不由 ATR 自动安装。服务器所有者可自行审查并手动部署于管理员控制路径，以 SSH ForceCommand 或管理员管理的 authorized_keys 强制命令调用。示意入口：

```text
restrict,command="/usr/bin/python3 /opt/atr-read/ssh_gateway.py --policy /etc/atr-read/policy.json" <client-public-key>
```

这是管理员配置示例，不是由 ATR 执行的命令。应按服务器 OpenSSH 版本检查 restrict/转发/用户 rc 限制，并确保账号无法修改 authorized_keys 或通过其他认证通道绕过。主机公钥由独立可信渠道核对，不能直接信任一次未知主机扫描。

策略文件示例（必须由管理员掌控、替换为真实授权范围）：

```json
{
  "services": ["catalog-api", "catalog-worker"],
  "allowed_paths": ["/srv/catalog/config"],
  "docker_reads_enabled": false,
  "containers": []
}
```

网关只接受固定 SSH_ORIGINAL_COMMAND，重新验证工具/参数/服务/容器/目录，固定 argv 且 shell=false。文件逐路径组件用 openat/O_NOFOLLOW，拒绝链接与特殊文件，配置只读有限字节；清单不递归。命令使用固定二进制路径、固定 PATH、期限/输出上限，容器 stderr 日志也纳入有界输出。systemctl 仅 show/list，不 start/stop/restart，journalctl 仅读。普通账号权限不足安全失败。

管理员还需检查 Linux 的二进制路径、systemd/journal 权限、策略所属权限、日志敏感性及账号其他访问面。模板不是自动证明：本机仅做网关策略单元检查与回环 SSH 协议测试，Linux openat、实际 journald/systemd 与强制账号配置未做真实运行验收。

Docker 在客户端和网关均默认关闭。如果确需 Docker，应另行设计管理员控制的受限只读 API/适当 rootless 环境并审核账号权限。标准宿主机 Docker socket/组基本等价主机高权限，禁止用授予该权限的方法宣称安全只读。

## 原 Collector 与本地访问

原 Demo Collector 仍需 Docker daemon，因此是显式 docker-demo profile，API 默认启动不依赖它。虽已 cap_drop=ALL、no-new-privileges、read_only、私有网络且无宿主机端口，socket 的 `:ro` 不能限制 daemon API 的写能力。被攻陷 Collector 仍可能控制宿主机，只能用于可信隔离本地 Demo。

API/UI 绑定 127.0.0.1，除最小 health 外 API 使用 OPERATOR_TOKEN，Collector 用另一令牌。工作台令牌保存在当前浏览器 sessionStorage，不写入交付包。单操作员认证不提供多人角色、租户隔离、证书管理或互联网发布安全方案。

健康验证只允许 ALLOWED_HEALTH_URLS 的精确 URL、无重定向、有限超时与响应字节。只有记录修复/人工复核和真实成功验证，再经知识审核才 Trusted；不会自动改变远端状态。
