# ATR V2.2 真实业务发现五项修复报告

日期：2026-10-11。工作区：`E:\ATR`。以本地未提交实现为基线；未连接正式服务器、未执行付费模型调用、未推送 Git。

## 交付范围与结论

五项客户端改造及两项公共能力已实现。无已确认映射时，已审核业务可以采集授权的基础清单，调用模型提出带证据引用的映射建议，持久化等待人工审核，再在**同一运行、同一预算**内继续深入调查。

现有 `/usr/local/sbin/atr-docker-read` **仅提供 list**。本轮实现固定 sudo 清单读取及能力拒绝，设计 status/logs 扩展协议，未安装或修改正式服务器网关/sudoers。不能声称生产容器日志权限已经验收。

完整回归、最终隔离验收和部署结果见本文末尾的验收记录与状态。正式现场 service.list 的历史 ValueError 根因仍未确定；合成兼容问题已被复现并修复。

## 1. BUG-01：systemd 清单兼容

原问题：列表解析器和工具参数分别使用限长 128 的名称正则，拒绝冒号及合法 `\xNN` 转义。列表任一格式异常即抛 ValueError，且丢失 load/sub/描述字段。

实现：

- `backend/app/connectors/unit_names.py` 共用名称验证；完整服务 unit 长度上限 255，兼容实例名、冒号和字面转义。保留旧有短服务标识兼容；参数不接受 Shell 拼接、控制字符或任意 flags。
- `models.py`、`standard_ssh.py`、`scope.py` 使用相同校验；游标同样支持这些标识。
- 固定 `/usr/bin/env LC_ALL=C LANG=C SYSTEMD_COLORS=0 SYSTEMD_URLIFY=0 SYSTEMD_PAGER=cat /usr/bin/systemctl ... --no-pager --no-legend --plain --full`，不依赖交互终端或语言环境。
- 单独解析 load、active、sub、description。最多跳过 16 个异常行；有效行可形成 partial 清单，坏行不进入可操作对象。全坏、异常数量超限、控制字符、整体超限或超时均不能成功。
- 服务状态/日志仍需当前有效的人工确认映射。每个 unit 作为一个固定命令参数引用，字面转义不会解码成 Shell 内容。

已证明：合成的 `worker@tenant:blue.service`、`worker@tenant\x2dblue.service`、255 字符 unit、部分坏行与恶意参数行为符合要求。未证明：正式服务器此前错误是否就是这些名称导致；本轮没有正式 SSH 会话。

## 2. BUG-03：现有受限 Docker reader

原问题：普通 SSH 的容器工具固定使用 Docker CLI，未提供现有 sudo 网关策略。

增加 `docker_reader=restricted_list`，旧配置默认仍为 `direct_cli`；普通 SSH 页面可选择策略，也可通过专门的 `PATCH .../docker-reader` 修改现有目标。切换策略会使容器映射失效，要求重新发现和审核。本轮未自动修改现有真实目标配置。

选择 restricted_list 后，container.list 只执行：

```text
sudo -n /usr/local/sbin/atr-docker-read list
```

`docker_reader.py` 有界解析名称、镜像、状态三列，支持 Tab、多空格及单空格分隔和 `NAMES IMAGE STATUS` 表头。每个容器带来源 `restricted_docker_reader`、协议 `legacy_text_list`、能力 `[container.list]`。名称作为 `identity_kind=name` 的候选身份，不伪造完整容器 ID。

非法记录被有界跳过或整体拒绝；输出大小、超时、目标累计调用次数以及调查预算均受限。不接收附加 sudo/Docker 命令，不静默回退，不加入 docker 管理组。container.status/logs 在参数与连接之前返回 `capability_unavailable`。

真实网关输出的完整样本未提供。本解析器针对明确的三列文本协议经过隔离测试；正式复测仍需确认实际格式，不能把 Fixture 当作正式网关输出。

### status/logs 扩展设计（未部署）

建议新增独立版本入口，例如 `atr-docker-read-v2`，由所有者审核后逐项授权。保留现有 list 入口及 sudoers 规则。

| 操作 | 固定命令及输入 | 服务端必须检查 | 输出 |
|---|---|---|---|
| list | 固定 list 操作 | 账号独立权限、对象范围、调用/时间/字节限制 | JSON 容器清单，完整 ID 与能力版本 |
| status | 固定 status 操作，stdin JSON 的 container_id | 完整合法 ID、服务端对象白名单、已确认业务授权 | 限定状态字段，禁止完整 inspect 敏感信息 |
| logs | 固定 logs 操作，stdin JSON 的 container_id、tail | 上述检查；tail 1–200；输出有界、敏感字段脱敏 | 有时间戳的有限日志 |

入口拒绝额外字段、任意 flags、Shell、写操作、SFTP、转发及 Docker socket 暴露；脚本与配置由 root 管理、低权限账号不可修改。每种操作分别获得 sudoers 或网关授权，不能因 list 可用推断 status/logs 已授权。服务端对象白名单与客户端已确认范围须一致，容器替换后重新核验。此为协议设计，本轮不声称实现了正式服务端扩展。

## 3. DESIGN-04：无映射自主发现与安全恢复

原问题：Agent 在没有当前 confirmed binding 时直接设置 permission_blocked，模型 not_called。轻量发现与调查预算分离，且普通 SSH 一个工具失败就停止后续发现。

新增 `agent/discovery.py`，复用 RuntimeService、Policy、BusinessToolRegistry、ScopeResolver、独立模型 Worker 和 ContextBudgetManager。

流程：

```mermaid
flowchart LR
  P[画像已审核] --> D[身份检查与基础发现]
  D --> E[脱敏证据与候选对象]
  E --> M[模型提出映射建议]
  M --> W[持久化等待范围审核]
  W --> H[人工确认]
  H --> V[重验版本、授权、凭据、映射及原预算]
  V --> I[深入状态、日志及授权配置调查]
  I --> G[严格根因证据门]
```

- 基础工具：host.info、host.memory、host.disk、network.listeners、service.list、已启用的 container.list、process.list。不在此阶段读取业务配置或候选日志。
- 无部署服务声明的已审核源码画像也可采集基础证据和调用模型，但不能猜测源服务映射。
- 模型契约与提示词来自同一个 Pydantic Schema：service、role、candidate_key、kind、evidence_ids、reasons、counterevidence、uncertainty、next_tools。
- 校验源服务及角色、确实采集的候选对象、属于该候选的证据引用、固定建议工具、重复建议。非法证据、额外执行指令、任意 Shell 和缺少业务身份依据的 PID 映射被拒绝。建议工具仅用于展示，不能直接执行或批准权限。
- 候选按服务、容器、进程排序，并按输入预算裁剪模型视图；底层观测保留。证据引用必须仍在模型实际收到的候选中。
- 状态映射：discovered 为发现记录；proposed 为 source_json 中的 AI 建议；awaiting_review 为持久化运行状态和 pending binding；confirmed/rejected 由人工审核；stale 由有效期和身份/授权变化判定。
- `POST /runs/{id}/resume` 复用原 run_id 和 budget_id。不能重复恢复已结束运行，不重新采集基础清单来绕过预算。再次检查画像版本、目标权限摘要、密码会话和当前映射。拒绝、过期、权限收缩或版本变化后不可恢复深入读取。
- 重新发现已过期/失效的同名对象不会自动恢复旧权限，必须再次审核。普通直接读取也拒绝失效映射的服务/容器深入操作。
- Agent 配置读取绑定当前已确认的服务/容器，并保留独立目录授权。操作者此前明确授权目录的手动读取通道仍按原目录权限检查；AI 不会通过它跳过候选审核。
- 发现证据单独记录 discovery_evidence_ids，不进入恢复后的根因证据上下文。不足以确定根因时返回 unresolved。

预算统一计入工具次数、有效或失败输出字节、模型输入/输出/实际 usage、模型次数及活动执行时间；恢复时不清零。新增执行时间预算默认 1800 秒，单工具和模型仍遵守原超时，未提高正式模型的 120 秒配置。没有自动重试。

## 4. BUG-02：进程噪声与对象分类

原问题：清单把各类运行对象平铺成裸 ID，“无关或未确认”混合展示；一个 PID 容易被误读成业务服务。

- 固定 ps 输出加入 stat，兼容已有二列协议（状态 unknown），保存名称、PID、状态。
- 发现结果增加 runtime_objects 和 process_groups，服务/容器优先，进程按名称/状态聚合。
- 页面分 systemd、Docker、Process 展示，进程默认折叠并分页，仍可展开每个 PID 及可引用观测依据。
- 未匹配、人工判定不相关、缺少身份依据、已确认分别展示；没有匹配不等于不相关。
- 人工拒绝同一身份的候选不会在下一次发现自动重新变成待审绑定。旧观测和历史候选保留。
- 聚合改变展示，不删除底层 evidence/runtime_observations；已有清单分页/输出上限保留，未采集的后续页不会冒充完整清单。

## 5. UX-05：图标和可选静态附件

原问题：所有图片均 pending_manual；文件清单只展示前 150 项，人工录入重复操作较多。

- 明确的 favicon、icon、app-icon、apple-touch-icon、logo、decorative 资源或图标目录，按明确路径规则标为 optional_static_attachment。此分类只表示附件用途，不推断业务结构。
- 架构、拓扑、部署、diagram 等关键路径及用途不明图片继续待审；无需图片模型或付费视觉调用。
- 可选附件不增加解析待办、不生成或确认业务事实，原文件、哈希、来源与版本保留。
- 新的 media-batch-review 接口支持最多 200 项的批量分类和撤销。校验业务归属、活动文件、图片类型、明确资源路径和当前版本；混入架构图/无效文件整批拒绝。
- parse_json 保留原分类与可撤销历史，已有 manual_text、Documented 事实及过去画像版本保留；撤销恢复原状态。
- 文件元数据与画像新版本在同一事务提交，使用版本比较，避免部分更新。前端文件清单按 50 项分页，支持明确图标批选、撤销及单独人工说明。
- 历史 pending_manual 图标可由操作者批量分类；本轮不改写当前真实 402 文件的分类或过去审核结果。

## 公共能力：安全错误与部分发现

统一 ToolFailure 契约包含 tool_id、execution_mode、error_code、failure_stage、status、line_number、output_bytes、invalid_item_count、retryable、partial、observation_id。诊断在 runtime_observations.diagnostics_json 持久化，并关联工具审计与页面。

诊断仅使用固定错误码/消息和数字位置，不保存异常原文、坏行原文、完整命令输出、Prompt、密码、密钥或 Authorization。旧错误记录保持不变，无法追溯的阶段不会被猜测补写。

普通解析/工具权限/网络失败可继续后续授权基础工具并汇总 partial；认证、公钥不匹配、身份检查失败或无法完成、凭据失效、明确授权撤销、目标/全局预算耗尽则停止。超时和整体输出超限不保存不完整证据。有效但不完整的清单明确 partial，不能证明未列出的对象不存在。

SQLite 迁移 24 仅追加诊断字段及预算 elapsed_ms，旧列和历史记录保持。升级前使用 SQLite backup API 保存一致的本地迁移恢复副本；不删除数据库或 Volume。

## 修改文件

业务与后端：`connectors/{models,policy,service,ssh,standard_ssh,unit_names,diagnostics,docker_reader}.py`；`system/scope.py`；`agent/{discovery,business_investigation,adaptive}.py`；`tools/business.py`；`context/{budget,model,model_errors}.py`；`storage/{database,migrations}.py`；`api/{business,v1}.py`；`business/{media,static,profile,models,service}.py`。

前端：`frontend/src/{business,main,runtime-inventory,media-review-list}.tsx`。

验收：`scripts/verify_ssh_password.py`；隔离 SSH Dockerfile、docker_read.py、model_fixture.py；新增 discovery_fixes、autonomous_discovery、inventory_grouping、media_optional、浏览器与 OpenSSH 验收测试。原范围测试中“失效映射仍可深入读取”的旧断言按新要求收紧；全工具失败 Fixture 加入新增 memory/disk 工具。没有删除原测试或减少覆盖。

## 验收证据与边界

开发分阶段通过后才进入下一阶段：service.list 85 项；restricted reader 单元覆盖及第一轮真实 OpenSSH 74 项；自主发现/既有调查 43 项并补测范围边界；PID 阶段 99 项；图片阶段 16 项；新增浏览器 2 项。

最终隔离验收使用独立 Compose 项目 `atr-real-discovery-test`、临时数据目录、合成密码和独立主机公钥。真实 OpenSSH 与实际 sudoers 验证 list、拒绝额外操作、密码/私钥、host key、root、SFTP/转发、到期/重启、旧 forced_gateway 协议。systemd/Docker 元数据由合成 CLI/reader 输出，**没有生产 Docker daemon 权限验收**。

Docker API 中的完整无映射流程另经内部 Fake HTTP Provider 和原独立模型 Worker 验证：7 次基础工具 + 1 次模型调用 → 等待审核；未确认日志拒绝；人工确认 → 同一预算执行 2 次深入工具 + 1 次模型调用 → unresolved。没有付费上游或正式服务器请求。

所有测试目录位于 `E:\ATR\test-results\real-discovery-20261011` 与 `real-discovery-ssh`。日志、XML、环境/数据摘要和测试工作流证明保留供核验。

### 最终测试结果

| 验收 | 结果 | 可核验证据 |
|---|---|---|
| 完整回归 | 420 passed，失败、错误、跳过均为 0；约 601 秒 | [完整回归 XML](E:/ATR/test-results/real-discovery-20261011/full.xml) |
| 完整回归后补充的边界 | 新增 3 项均通过：无部署服务的源码画像、仅有进程绑定、密码会话过期后恢复拒绝及重新提供密码后沿用原预算 | [源码与进程边界](E:/ATR/test-results/real-discovery-20261011/final-source.xml)、[密码恢复边界](E:/ATR/test-results/real-discovery-20261011/final-password.xml) |
| 相关边界复验 | 53 passed，失败、错误、跳过均为 0；与完整回归及追加用例有重叠，不累加为完整回归数 | [专题回归](E:/ATR/test-results/real-discovery-20261011/final-edge.xml) |
| 最终冻结镜像的 Linux/OpenSSH 验收 | 75 passed，0 失败、0 错误 | [OpenSSH XML](E:/ATR/test-results/real-discovery-ssh/acceptance-1e24fb74/junit.xml) |
| 新增页面流程 | 2 passed；实际验证映射审核/恢复及图片批量分类/撤销 | [浏览器 XML](E:/ATR/test-results/real-discovery-20261011/phase5-uib.xml) |
| 静态检查及前端构建 | Ruff 全部通过；前端构建退出码 0 | [前端构建记录](E:/ATR/test-results/real-discovery-20261011/frontend-final.log) |
| 本地部署后健康 | API Docker health=healthy，API HTTP 200；Web running，HTTP 200 | [部署校验](E:/ATR/test-results/real-discovery-20261011/after-deployment-proof.json) |

准确的口径是“完整回归 420 项通过，随后追加 3 项边界通过”，不是声称一次完整回归运行了 423 项。

最终镜像中的测试调查 `264254df-b372-4231-b1b7-6dd812084b02` 使用内部合成 HTTP Provider，由原独立 Worker 发出 2 次模型请求、执行 9 次工具调用；原预算 ID 保持 `d9e45aca-698e-4789-a582-9168655565c2`，未授权日志读取被拒绝，最终正确返回 unresolved。[工作流证明](E:/ATR/test-results/real-discovery-ssh/acceptance-1e24fb74/discovery-workflow-proof.json)

这里的模型请求均为隔离测试 Provider 调用，不是正式付费上游。隔离 SSH 测试项目已停止；正式本地 API/Web 保持运行。

### 本地部署与数据保留证明

仅用独立镜像覆盖文件更新 API/Web，沿用现有 Compose 配置、环境参数、端口与挂载。未修改 `.env`、现有 Compose 文件或旧部署覆盖文件。本轮新增的覆盖文件为 [deployment-compose.yaml](E:/ATR/test-results/real-discovery-20261011/deployment-compose.yaml)。

| 项目 | 部署结果 |
|---|---|
| API | `atr-real-discovery-api:20261011`；镜像 `sha256:12af1696357b8802d208e0d208f85bfc9b5274e183bfa7376e5a0c4485dabe53`；端口 28000 |
| Web | `atr-real-discovery-web:20261011`；镜像 `sha256:037dc1c79686b6a5c380a606fd9a3145535e45f446b42b8560099f1819bc7bf8`；端口 28080 |
| 原数据卷 | `atr-runtime_runtime-data` 保持原卷，不删除、不重建 |
| 旧镜像 | 保留，并添加 `atr-runtime-api:before-real-discovery-20261011`、`atr-runtime-web:before-real-discovery-20261011` 标签 |
| 数据迁移 | 追加迁移 24；一致的 `.pre-discovery.bak` 副本存在；SQLite integrity=ok，外键检查无异常 |
| 模型 Worker | 原隔离 Worker 源码哈希保持 `11d715359a4700a1b9f66d5bf0271a780bde00d883060740e44681613da89054` |
| Git | HEAD 仍为 `0b2ed815d259b84500bafb484c69e0fc29c48be9`；旧未提交实现保留；本轮无 push/merge/PR |

升级前后逐表比较原有列、行数与内容哈希：42 张原表全部一致；迁移记录只新增版本 24。402 条 artifact_files 记录、1 个业务画像及其 5 个历史版本、1 个目标、6 条真实观测、1 次调查、1 次发现、6 条任务及其原字段均未改写。原附件目录内 405 个物理文件的路径、字节数及内容哈希一致；它们与 402 条文件解析记录是不同计数。部署时及验证时活动任务均为 0。

容器环境摘要、端口和挂载语义保持一致。Docker Desktop 重建后将 Windows 挂载来源 `E:\ATR\demo`、`E:\ATR\targets` 显示为 `/run/desktop/mnt/host/e/ATR/...`；校验按同一目录规范化比较，并确认只读属性与传播设置不变。数据卷的身份与挂载保持一致。这是路径表示变化，不是改用其他目录。

[升级前数据摘要](E:/ATR/test-results/real-discovery-20261011/before-data.json)、[环境基线](E:/ATR/test-results/real-discovery-20261011/before-environment.json)和[升级后证明](E:/ATR/test-results/real-discovery-20261011/after-deployment-proof.json)保留了核验所需的计数与哈希；不包含业务原文或密钥。

## 正式复测前的操作

1. 本地 API 升级会清除内存密码会话，按原架构重新提供低权限账号密码；不会保存密码或自动使用 root。
2. 在现有目标选择“受限网关（仅清单）”。这只改变客户端策略并使旧容器映射待复核；不修改正式 sudoers。本轮未自动更改真实目标。
3. 发起新调查，检查 service.list 的安全诊断及实际三列 list 格式。单次成功不足以认定历史 ValueError 的现场原因已证明。
4. 审核 AI 提出的具体业务映射；名称、镜像与端口只是线索，其他项目仍无日志/配置授权。
5. 现有网关不提供容器 status/logs。正式扩展必须另行取得所有者授权、分别审核网关和 sudoers 后验收；在此之前 container 深入调查明确不可用，不能宣称生产全能力就绪。

## 最终交付状态

```text
IMPLEMENTATION_STATUS=completed_local; server_capabilities_partial
BUG_01=compatibility_fixed; historical_production_cause_unconfirmed
BUG_03_LIST=passed_isolated_fixed_sudo_gateway
BUG_03_STATUS_LOGS=capability_unavailable; extension_design_only
DESIGN_04=passed_discovery_review_resume
BUG_02=passed_grouping_and_classification
UX_05=passed_optional_batch_and_undo
STRUCTURED_TOOL_ERRORS=passed
PARTIAL_DISCOVERY=passed
AGENT_WITHOUT_BINDING=passed_test_provider_called
UNAUTHORIZED_LOG_READS=denied
FULL_REGRESSION=420_passed; 3_additional_boundary_tests_passed
OPENSSH_ACCEPTANCE=75_passed
API_HEALTH=healthy; HTTP_200
WEB_HEALTH=running; HTTP_200
DATA_PRESERVED=true
PRODUCTION_CONNECTIONS=0
PRODUCTION_MUTATIONS=0
REAL_MODEL_CALLS=0
GIT_PUSHED=false
READY_FOR_REAL_RETEST=true_for_discovery_and_list; container_status_logs_unavailable
```
