# 只读网关 AB v1 升级与回退

新协议 `atr-read-ab-v1` 先协商工具版本和输出/时间限制。旧网关只在已验证的 JSON 形状下适配；不支持 HTTP/启动周期时保留明确缺口，乱码或不兼容版本不适配。旧调查仍使用原协议。

服务器包为 `scripts/ssh_gateway.py` 和 `backend/app/connectors/remote_reads.py`，部署时后者放到网关同目录，名称 `remote_reads.py`。两份代码仅使用 Python 标准库。新增读取不执行用户源码，不请求 Docker 环境变量或健康日志，不改变业务服务。

`health.get` 只接受 endpoint_id。管理员服务器策略和 ATR 目标配置分别保存相同的精确 URL，均限定当前目标的远端观察位置；普通 SSH 在远端运行同一读取代码。请求不使用代理、不跟随重定向，不接受凭据、查询串、fragment 或模型提交的 URL。配置端点不会自动授予服务器权限。

容器状态记录完整容器 ID、StartedAt 和启动周期摘要；日志限制为同一次启动，前后重新读取身份，发生重启则输出 partial。systemd 以 InvocationID 筛选 journal，保留原 JSON 行和 UTC 时间。周期或时间缺失不能据此完成健康或完整故障链核验。

维护脚本 `scripts/gateway_upgrade.py` 使用现有 OpenSSH 管理别名，BatchMode 和 StrictHostKeyChecking，要求当前网关/策略精确摘要。固定服务器路径为 `/usr/local/libexec/atr-read-v1/ssh_gateway.py`、`/etc/atr-read-v1/policy.json`，固定受限账号为 `atr-read-v1`。其他服务器须先由管理员核对并调整脚本，不通过模型参数选择路径或账号。

依次运行 check、apply；须指定 `--ssh-alias`、唯一 `--backup-id ab-...`、`--expected-gateway-sha`、`--expected-policy-sha`、经审阅的 `--endpoints-file` 和本地 `--report`。check 不写服务器；apply 保存原程序/策略/元数据和文件摘要，在原目录原子替换并 fsync，然后使用真实低权限账号验证能力、授权容器与 HTTP。任一检查失败自动恢复原程序与策略。操作需要当前操作者的维护授权，普通 ATR 调查没有管理入口。

回退执行同一脚本 rollback，并传入相同别名、备份 ID、报告路径。回退先验证当前文件仍是本次版本，拒绝覆盖管理员后续变更；恢复匹配原文件/权限并核对业务容器状态和启动时间。新增 helper 保留为未被旧网关使用的 root 管理文件，不删除生产资料。备份位于 `/var/backups/atr-read-v1/<backup-id>`，root 0700；需与相应 ATR 镜像/数据库备份共同保留。

HTTP/read-source 授权变更使原调查的授权摘要失效，应创建新调查；waiting_input 的答案不能绕过这一检查。授权取消、密码会话到期、进程取消和父租约失效时不接收晚到证据。
