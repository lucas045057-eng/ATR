# ATR V2.2 全流程验收与闭环修复

日期：2026-10-12（北京时间）。本报告记录实际执行结果；工程联通、真实自主推理与已验证故障根因分别判定。

后续进度说明：操作者于下一轮契约修复中授权同步 GitHub 并推送 main。本文“不 Push”等为本轮当时的执行记录；最新修复、部署与同步情况见 `model-decision-contract-unification-report.md`。

当前交付：新建 Run 的输入/累计预算已按操作者选择部署为 **30000/200000**，保留 180 秒请求、190 秒 Worker、8 次模型、20 次工具及零 SDK 重试。工程链路已验收，真实模型稳定 RCA 尚未通过。下列各版本旧预算及测试数字是历史执行记录，最终版本验证另见 5.13；不将旧结果冒充当前部署。

## 1. 原始问题与证据

本轮接手时保留了原有未提交修改，并将其复制到 `test-results/final-e2e-20261012-8336e93a/pre-existing-changes`。没有覆盖此前的合法节点候选、字段级诊断和生产配置读取工作。

最新历史调查 `3fd72a1d-c125-4100-904e-873325ffdb4c` 的第二次模型请求 `cf792cd5-a05c-4aa9-89da-bbaf4e0038ab` 在 `hypotheses[0].verification_predictions[0].target_node_id` 触发 `unknown_prediction_target`。原始非法值没有持久化，无法确认它具体是什么，也不能宣称恢复了历史生成原因。已有严格校验在执行后续工具前拒绝了该输出。

可复现的接口问题：根假设及嵌套预测的节点、证据和动作字段，在模型输入 Schema 中原本只是任意字符串；合法候选约束主要依赖文字说明。另发现密码会话失效继承 `PermissionError`，调查层会把它概括为服务器权限拒绝；通过离线第三次读取到期案例复现。

证据门另有明确契约：只有 `startup_failure`、`permission_denied`、`bind_failure`、`database_unavailable`、`redis_unavailable` 具有内置机制核验规则。历史模型使用过自然语言故障名称。不能由此断言历史根因应当成立，但模型应当知道哪些机制名称可进入现有确定性核验。

## 2. 本轮最小修复

| 文件 | 改动与目的 |
| --- | --- |
| `backend/app/agent/model_schema.py` | 从 `allowed_service_nodes(state)` 动态生成共享的 `AllowedServiceNodeId`，同时约束根假设与嵌套预测；公开有效证据、动作、证据门机制名称；真实验收后进一步将预测工具/字段候选收窄到已有观测或可执行候选能提供的字段，并解释反证必须针对同一因果链。 |
| `backend/app/agent/workflow.py` | 明确全部引用契约；精简重复 Prompt、历史假设 ID 和模型预览中的采集 call_id；在输入预算 90% 时提前压缩证据文本预览，仍由原预算检查决定是否允许发送。 |
| `backend/app/agent/verification.py` | 预测工具/比较方式/值域统一为共同规则，供原校验器和决策 Schema 使用；原严格语义保持不变。 |
| `backend/app/agent/business_investigation.py` | 将会话失效准确分类为 `ssh_credential_unavailable`；区分供应商超出预留、执行时间、资料量与工具次数预算，避免错误归因为工具次数耗尽。 |
| `backend/app/tools/business.py` | 工具审计保留凭据失效专用错误码；其他既有异常类型保持兼容。 |
| `backend/app/context/model_errors.py` | 保留已有安全诊断修改，并增加跨节点反证专用安全错误类型，不记录非法值。 |
| `frontend/src/model-error.tsx` | 嵌套未知预测目标显示“不在本轮允许引用的服务节点集合中”。 |
| `tests/test_final_reference_contract.py` | 有限引用、空候选、嵌套非法 ID、压缩余量、凭据失效与审计一致性回归。 |
| `tests/test_budget_diagnostics.py`、`tests/test_model_output_compatibility.py`、`tests/test_v22_adaptive_edges.py` | 新旧 Run 独立预算、旧上限严格拒绝、新预算完整引用、历史反证保留及供应商用量异常的准确停止原因。 |
| `tests/integration/bounded_decision_experiment.py`、`tests/integration/test_bounded_decision_experiment.py` | 仅在隔离实验验证一次有界语义纠正；没有进入正式 API 或生产调查。 |
| `.env` | 仅将新建 Run 单次输入预算提高到 30000，累计预算提高到 200000。 |
| `tests/integration/final_e2e_acceptance.py`、`tests/fixtures/final-e2e/*` | 独立真实 Docker、OpenSSH、数据库及可恢复 API 故障验收；付费 Provider 必须显式选择，默认只有 Fake Provider。 |

此前已有的 `tests/test_target_reference_contract.py`、`tests/e2e/test_model_errors.py` 和相关报告继续保留。

前六次实验未修改 `.env`、Compose、供应商接口、模型隔离方式、正式调查的重试策略或生产权限。随后操作者明确选择新建 Run 单次输入 30000、累计 200000，本轮仅修改 `.env` 的这两个预算字段，见 5.10。正式 API 没有增加自动纠错请求；仅隔离实验有一次语义纠正包装器，详见 5.7。动态 Schema 放在决策 Prompt 中，HTTP 仍使用既有 JSON 输出方式；它提高契约清晰度，并不保证供应商必定遵循。因此全部严格引用和动作校验继续保留。

## 3. 引用、压缩与安全一致性

- 候选与校验共用完整的本轮 service 节点集合；排除 kind=datastore 和 runtime 节点。角色仍须精确匹配。
- 超过 30 个合法服务节点时明确返回 `target_candidate_limit`，不截断候选，不发送模型请求，不执行模型决策工具。
- 服务名、容器 ID、SSH Target ID、动作 ID、假设 ID 和其他图节点 ID 均不能代替合法 node_id；所有嵌套预测逐项校验。
- 证据、动作引用必须属于当前状态；模型不得提供工具参数或 Shell 命令。后端仍校验动作组合、业务范围和 Run 的唯一 target_id。
- 压缩只改变模型预览，原始证据、采集 call_id、observation_id、历史假设与审计仍保存在原状态和 SQLite；证据门继续使用完整原始证据判断独立采集、故障窗口、反证和机制。
- 预览压缩到最小宽度仍超预算时继续拒绝请求。后续经操作者明确选择，仅将新 Run 上限调整为 30000/200000；不升级历史 Run，不继续精简输入，不绕过任何预算检查。

异常边界的可重复验收来源如下，均使用本地 Fake/无网络测试，不消耗生产模型额度：

| 边界 | 测试证据 |
| --- | --- |
| 根节点、嵌套预测、证据及动作非法引用 | `test_target_reference_contract.py`、`test_final_reference_contract.py`、`test_model_output_compatibility.py`；非法决策后无后续工具。 |
| 密码失效、工具授权拒绝 | `test_ssh_password.py`、`test_final_reference_contract.py`、`test_v22_adaptive_edges.py`；专用停止原因与审计一致。 |
| Timeout、HTTP 500、非法 JSON/Schema、Worker 硬超时 | `test_model_isolation.py`、`test_v22_model_http.py`、`test_model_output_compatibility.py`；保留失败审计、释放预算、不自动重试，API 仍可服务。 |
| 输入/累计预算、空证据、不足证据及伪独立观测 | `test_budget.py`、`test_budget_diagnostics.py`、`test_v22_verification.py`；不发起越限请求，静态和历史内容不能证明当前故障。 |
| 映射失效、跨目标污染与重复任务 | `test_run_target_binding.py`、`test_v22_adaptive_edges.py`、`test_v22_job_edges.py`；不回退旧目标，保留唯一运行与预算。 |
| 暂停、人工审核、恢复及进程重启 | `test_v22_jobs.py`、`test_v22_job_edges.py` 和隔离 Linux/OpenSSH 验收；恢复原 Run 的目标/预算，过期凭据不执行命令。 |
| 页面最终状态与安全字段级错误 | `tests/e2e/test_model_errors.py`；真实 Chrome 显示安全路径、错误类型及预算原因，不回显原始非法值。 |

## 4. 历史版本回归及本地部署

首轮全量回归：452 通过、12 失败。11 项是默认 Playwright 浏览器未安装，改用本机 Chrome 后全部通过。另 1 项发现错误码兼容性回归，已收窄为只保留凭据专用错误码。最终全量回归：**464 通过，0 失败，681.20 秒**（含 453 项后端及 11 项浏览器）；随后新增嵌套预测字段浏览器案例，格式/根引用/嵌套引用共 **3 通过，24.67 秒**。全部无需付费验收通过后才开始真实模型实验。

真实试验暴露的预测契约及旧预算兼容问题修复后，最终版本再次全量验收：**466 通过，0 失败，696.42 秒**，包括 454 项后端与 12 项 Chrome 浏览器验收；`full-acceptance.xml` 保存逐项结果。Ruff 和 `git diff --check` 通过。

专项引用与自适应调查：38 通过。前端 TypeScript 与生产构建通过。隔离 Linux/OpenSSH、密码、私钥、只读命令、Docker Mock、异步调查及审核恢复验收：75 通过，证据目录 `test-results/real-discovery-ssh/acceptance-5c086e3f`。该套 Docker CLI 为合成夹具，真实 daemon 故障验证另见下一节，不能混为真实业务证据。

本地已重新创建 `atr-runtime` 的 API 和 Web；未执行 down，未删除 Volume。API healthy；API `/api/v1/health` 与 Web 入口均返回 HTTP 200。部署前活动任务、调查、模型请求、工具调用及模型 Worker 数均为 0。

此前预测契约版本回归后只更新 API。跨节点反证修复的最新版本已再次全量验收：**471 通过、0 失败、699.18 秒**，包含新增 4 项后端和 1 项浏览器案例，结果为 `full-scoped-counter.xml`。TypeScript、前端生产构建及 Python Ruff 通过。更严格的真实 Docker + Fake 真值/证据验收 **2 通过、53.948 秒**，结果为 `fake-strict-rca.xml`。

最后重新创建本地 API/Web，部署前调查、任务、模型、工具、发现及观测活动全部为 0。容器内七份相关源码 SHA-256 与本机一致，API healthy、API/Web 200。重建前后的 42 张表内容摘要完全相同，原 Volume 与 405 份资料摘要一致；`.env` 未改。API 重建使内存 SSH 密码会话失效，下一次生产使用需从正常页面更新。

运行 Settings：`gpt-6.1-sol`、`chat_completions`、`low`、请求 180 秒、输入 20000、累计 100000、输出 1500、模型 8 次、工具 20 次。通过无网络拦截进程启动，验证 Worker 请求 180 秒、父进程硬超时 190 秒、重试 0。没有发起真实模型请求进行配置验证。

最后的必要缺口修复版本：**474 通过、0 失败、712.32 秒**，其中 **461 项后端、13 项 Chrome 浏览器**，证据 `full-pending-final.xml`；SSH/Docker Mock **75 通过、0 失败、29.630 秒**；真实 Docker + Fake 严格真值验收 **2 通过、55.91 秒**。无需付费验收全部通过后只重新创建 API，不再次重建 Web。七份容器源码与本机一致，180/190 秒与原预算实际值保持不变；API healthy、API/Web HTTP 200。42 张表、405 份资料及 `.env` 摘要均与重建前相同。安全部署验证 `final-deployment-verification.json`。

## 5. 独立 Docker 正常与故障场景

隔离实验的 ATR 应用使用本机 Python/FastAPI TestClient 与独立模型 Worker，生产只读 Run 则运行在本地 Docker API 容器；两者使用同源代码及明确核对的模型配置，操作系统和应用宿主并不完全相同，不能称为完全相同的进程环境。资源使用唯一 `atr-final-e2e-*` 名称和同名标签、独立网络、临时 SQLite；测试数据库使用 tmpfs，没有挂载正式 ATR Volume。OpenSSH 只绑定回环端口，主机公钥固定，低权限账号通过 ForceCommand 进入现有网关。网关只能读取白名单 API/DB 状态日志和 `/srv/final-reviewed`。清理前核对名称、标签及没有 Volume 挂载，只清理本轮创建的测试容器和网络。

真实故障真值：PostgreSQL 在 5432 正常接受连接并成功执行 SELECT 1；测试 API 实际被配置为连接 store:25432，启动连接拒绝后退出。真值由测试注入记录保存，未作为模型答案或症状传入。模型可以通过授权工具获取实际配置快照和日志。

正常对照使用 5432，独立 HTTP 检查返回 200，数据库 SELECT 1 成功。故障恢复仅在隔离环境启动正确端口的 API，恢复 HTTP 200 并再次验证数据库 SELECT 1。

Fake Provider 通过真实 HTTP、独立模型 Worker、真实 SSH 和 Docker 工具做编排验收。它从实际观察中选择工具和构造报告，因此只能证明工程编排与证据门，不计为真实 AI 自主诊断。正常场景 unresolved 表示没有已证实根因；故障场景只有窗口内独立状态、机制日志和可验证预测齐备后才 supported。首次夹具故障日志早于调查窗口，证据门正确拒绝；修正事件开始时间覆盖真实注入时刻后通过，未放宽窗口规则。

最终低推理 Fake 编排：正常与故障共 2 通过；两个场景各 6 次决策、7 次真实工具采集，故障恢复 HTTP 200。详细 Run ID、逐轮决策、预算及工具审计保存在 `test-results/final-e2e-20261012-8336e93a/isolated-5432.json` 和 `isolated-25432.json`。

最终 Prompt 精简后重新执行真实 Docker 场景：`actual-docker-acceptance.xml`，**2 通过，54.31 秒**。复验完成后，本轮标签 `atr.final_e2e` 的容器及网络均为 0；保留镜像和验收文件，未删除正式数据卷。

### 5.1 真实 GPT 隔离验收（不计为生产 Run）

Run：`708a0c82-f9fe-41c0-9336-165fde482169`；SSH 测试目标：`49a87f27-eaae-4250-bca3-effa12fa830e`。实际使用现有 gpt-6.1-sol、Base URL、API Key、low、180 秒、零重试；一次新隔离 Run，没有自动模型纠错或重复发起付费实验。

六次调用均成功完成解析与严格引用校验。供应商报告输入合计 **58763**、输出 **4585**，费用未知；缓存 Token 未被现有调用审计单独保留，不能填成 0。本地输入估算仍是 UTF-8 字节与固定开销，不等于下表 actual usage。

| 轮次 | 本地输入估算 | 供应商输入/输出 | 总耗时 ms | 网络 ms | 初始化 ms | 模型选择的后续工具 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 10875 | 9062 / 767 | 34016 | 31343 | 2422 | DB container.status |
| 2 | 11401 | 9204 / 563 | 10437 | 7782 | 2406 | DB container.logs |
| 3 | 12685 | 9708 / 597 | 11610 | 8906 | 2453 | config.list |
| 4 | 13728 | 10113 / 900 | 110985 | 108297 | 2437 | config.read |
| 5 | 14552 | 10366 / 992 | 42297 | 39609 | 2438 | code.search |
| 6 | 14570 | 10310 / 766 | 72453 | 69782 | 2421 | 无后续动作，安全停止 |

SDK/Worker 重试审计全部为 0；供应商内部处理不可观测，不据此断言其内部没有重试。成功调用未保存明确 HTTP 状态，依据有效响应、解析校验和 actual usage 判断调用成功。

7 次工具全部 succeeded、全部绑定同一测试目标；其中 6 条运行证据，另 1 次为 Static 代码检索；读取合计 7288 bytes。原始证据编号：

- API 状态：`5401c34f-d039-44f8-8795-8aad900a61b7`。
- API 启动失败日志：`b7d535ac-548f-4dba-8089-479bcfdeff38`。
- DB 状态：`5578a171-9fb4-4ea1-b194-6ea539c4a1e2`。
- DB 日志：`1a09f985-12c5-4c17-9609-b04f68739c0b`。
- 配置目录清单：`c77db6a6-6e73-4057-a588-a7052d028e6c`。
- 审核配置快照：`52335e4c-18ab-403a-8fa2-a2ee7ad1c296`。

推理轨迹：先提出数据库不可用假设，DB running 与就绪日志出现后保留反证，进一步选择配置清单、配置内容和静态代码。最后正确识别 API 连接 store:25432 被拒绝、DB 日志显示 5432、审核快照两端口不同，但对可能存在转发层保持不确定。它没有提前获得标准答案，也不是固定脚本式 Fake 回复。

各轮最终门控均针对 API 的 `database_unavailable` 候选，模型置信度不能覆盖证据门：

| 轮次 | 门控状态 | 支持/反证条数 | 预测核验 |
| --- | --- | --- | --- |
| 1 | candidate | 2 / 0 | DB 状态、监听均 missing。 |
| 2 | refuted | 2 / 1 | 预测缺失或不可实施；保留 DB 正常反证。 |
| 3 | refuted | 2 / 2 | 新增 DB 日志反证，继续寻找配置。 |
| 4 | refuted | 3 / 2 | API/DB 状态 verified，监听 missing。 |
| 5 | refuted | 5 / 2 | service.logs 与监听预测 missing。 |
| 6 | refuted | 3 / 2 | API exited verified，监听 missing；不再执行工具。 |

**最终 unresolved / evidence_conflict，严格 RCA 验收未通过。** 机制日志与独立退出状态已被证据门核验，但预测包含本轮无法采集的 `network.listeners/listening`，且历史反证仍阻止确认。同一笼统 `database_unavailable` 假设中将 DB 正常作为反证，也不能自动解释为 API 端口错误被反驳或擅自删除该反证。

### 5.2 真实验收发现的最小契约修复

`AdaptiveInvestigationPlanner.candidates()` 本轮只生成映射状态/日志、配置及代码读取；现有 SSH `network.listeners` 返回文本或 listeners 清单，没有服务节点级 listening 布尔值。第 5 轮还使用 service.logs 预测，而实际映射是 container。原 Prompt/Schema 将这些注册工具统一列为预测选项，容易诱导无法兑现的预测。该问题有实际合法决策与代码证据，已用离线案例复现。

修复仅限模型输入契约：从已有可核验观测和当前可执行候选动态推导预测工具与字段；文本/清单不会被伪造为 listening 布尔值；不可用检查必须写入 missing_evidence。进一步说明“DB running 反驳停机，不反驳应用连接或端点配置错误”，保持原反证记录及严格证据门。没有实现新的监听、连接探测或扩大生产权限。

修复后相关回归 68 通过，真实 Docker + Fake 两类场景 2 通过。随后全量回归 465 通过、1 失败：新增说明使旧 12000 输入预算的多轮中文案例第二轮最小预览达到 12141，预算检查正确阻断。精简重复 Prompt 说明后，该案例两轮估算为 11738 / 11967，3 个合法节点和 8 条证据编号均保留，预算未调整。压缩的 90% 是目标余量，最小预览仍可能高于它；原硬预算继续决定是否允许请求。最新完整回归另行验收。

原真实试验在诊断断言失败后安全清理了全部自身容器和网络；其 recovery_http_status 保留 null。正确端口恢复 HTTP 200 和 SELECT 1 的证明来自独立 Fake 编排的真实容器场景，不能冒充第一次真实模型试验执行过恢复。操作者后续批准一次额外真实隔离复验，其独立结果另行记录。

### 5.3 已批准的一次真实隔离复验

生产 Run 完成后，使用修复后的同一版本启动一次额外隔离 Run：`0e1d4725-9e45-4cbc-b71d-ebf70817bc9f`，测试 Target：`37ec9f4c-1a53-487d-bc6e-da9221b0b417`。没有与生产付费请求并发。独立真值仍为 API 使用 25432、真实 PostgreSQL 使用 5432，模型未提前获得注入答案。

**实际验收断言失败：1 failed、1 deselected、289.77 秒。** 不是超时、权限或引用失败：8 次真实模型调用全部 succeeded，9 次工具全部 succeeded（8 次真实 SSH、1 次 Static 代码检索），8 条运行证据、读取 7741 bytes。所有远程工具 actual_target_id 都是上述隔离 Target；Static code.search 的 actual_target_id 为 null，不能说它也执行了 SSH。

| 轮次 | 输入估算 | actual 输入/输出 | 总耗时 ms | 网络 ms | 后续动作 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1 | 10859 | 9042 / 765 | 39375 | 36687 | DB status |
| 2 | 11824 | 9278 / 514 | 9954 | 7297 | API config.list |
| 3 | 13422 | 9774 / 648 | 14797 | 12140 | API config.read |
| 4 | 13318 | 9735 / 847 | 30297 | 27625 | DB logs |
| 5 | 13994 | 10052 / 822 | 16782 | 14094 | DB config.list |
| 6 | 14563 | 10329 / 1329 | 44719 | 42062 | DB config.read |
| 7 | 15417 | 10571 / 787 | 42796 | 40125 | Static code.search |
| 8 | 14993 | 9003 / 1709 | 81828 | 79156 | 达到 8 轮后停止 |

初始化 2406–2437 ms，解析计时精度下为 0 ms，重试全部 0。实际用量输入 **77784**、输出 **7421**、累计 **85205**，未达到 100000。最终 unresolved / round_limit，未发生预算或 180 秒请求超时。

第 8 次供应商报告 completion_tokens=1709，超过配置的输出请求值 1500。代码仍以 `max_tokens=1500` 传入 Worker（`backend/app/context/model.py:135`），没有在本輪提高该值；现有审计不保存推理/可见输出 Token 细分，也不能证明供应商如何解释限制。必须按实际 usage 结算，不能宣称付费输出始终严格不超过 1500。供应商计量与本地估算不同这一限制仍存在。

模型最终明确指出 **API 连接 store:25432、PostgreSQL 在 5432、审核配置端口不一致**，并承认有效运行环境和实时连通性未验证。因此它找到了与真值一致的强候选，但**不能称为严格 RCA 确认通过**。

可核验的首次阻断出现在第 2 轮：API 的 `database_unavailable` 假设将 DB running 证据 `890ad570-6b4d-4275-a38d-3b4b65aef599` 标为 refutes，同时其解释明确说“反驳数据库停机，但不反驳端点或连接故障”。字段关系与因果解释不一致。此前有限 Schema、清晰引用与可用工具预测约束没有解决这种语义错误。

`workflow.py:201` 继续保留同目标/同 failure_mode 的历史反证；`adaptive.py:129` 用 target_node_id + failure_mode 形成假设身份，`:134` 合并历史反证；`workflow.py:338` 的最终门控要求没有未解释反证。第 5、8 轮机制与预测已核验通过，仍被这个未解决的历史反证拦截。不是 DB 映射错误，也不是未知节点误拒绝。责任边界：模型证据关系错误，以及较粗的假设身份/反证解释能力限制；证据门按既有契约保守停止，没有将猜测当事实。

关键证据：API exited `238cf08c-ccb9-4cbe-ae86-0650cdaded9a`；连接拒绝日志 `372aeda6-bab0-4729-adb3-3e9839dc7484`；DB running `890ad570-6b4d-4275-a38d-3b4b65aef599`；DB 日志 `9feaa09c-42e1-4289-aa02-92e9a742a961`；审核配置 `37bf2ea4-1222-46f8-b7ee-bdfd67217577`、`da9416e2-4902-4b2a-abd3-1d1671e9887d`。两次读取同一审核副本不构成两份独立当前因果证据。

隔离服务恢复已实际完成：测试 API 改为 5432 后 HTTP 200，真实测试数据库再次 SELECT 1 成功。恢复步骤在最终诊断断言之前执行，记录 recovery_http_status=200；清理后本轮标签容器/网络均为 0。未操作生产容器或正式数据卷。证据文件为 `real-retest/real-isolated-trial.json`、`real-retest/safe-analysis.json`、`actual-docker-real-retest.xml`。

该额外授权 Run 的 8 次请求已使用完，其失败结果原样保留。操作者随后明确要求“不用在乎费用限制”，授权继续有依据的修复及真实模型复验；不据此取消任何单 Run 预算、次数、超时或安全边界。

### 5.4 跨节点反证的确定性复现与最小修复

继续验收前补充 4 个无需付费案例：原代码 2 通过、2 失败。可复现的是 `validate_report()` 接受 DB 节点的证据作为 API 假设反证，然后继续执行 config.list；错误关系可以污染本 Run 历史。它不是数据库映射或 SSH 授权故障。

最小修复收紧新的模型决策契约：refutes 证据的 target_node_id 必须等于其假设 target_node_id；依赖自身的停机方向须在依赖节点单独建立假设，依赖证据仍可提供相关支持。违反时返回 `hypotheses[*].evidence_links[*].relation / counterevidence_target_mismatch`，在执行下一工具前停止。没有自动改写关系、丢弃反证、变更已有历史或调整证据门。

Schema 说明区分“应用连接依赖失败后退出”的 startup_failure 与“数据服务自身不可用”的 database_unavailable；调查 Prompt 要求先核对依赖运行和审核配置，单独确认启动失败不能作为根因证明。隔离验收增加根因真值对照：supported 还必须明确解释实际的 25432 与 5432，并发生配置/日志检查，不能只以通用 startup_failure 让测试通过。

专项 112 项中 111 通过，1 项是新增说明占用了旧 12000 预算；精简描述后旧预算 3 项重新通过。新安全错误的浏览器四类验收 4 通过、34.39 秒，真实 Docker + Fake 正常/故障 2 通过、55.19 秒，前端构建与 Python Ruff 通过。当时 API/Web 镜像的 Linux/OpenSSH、Docker Mock 与浏览器验收 75 通过、29.215 秒，目录 `test-results/real-discovery-ssh/acceptance-be29ed02`。该版本全量 471 项通过，更严格的数据库日志及配置引用真值验收 2 项通过后完成本地部署。第三次真实隔离复验结果见下一节。

### 5.5 第三次真实隔离复验：提前终止的确定性缺陷

Run `b1ffe945-7019-4f10-a4c6-a0ea96b03361`，测试 Target `0c177d4c-13d6-447a-a9d1-8a478dfdd874`。本次只调用真实 GPT **1 次**，actual 输入 **9082**、输出 **463**，总耗时 **18891 ms**、网络 **16187 ms**、初始化 **2438 ms**、重试 0。两次 API 状态/日志工具成功；隔离故障恢复 HTTP 200、数据库 SELECT 1 成功，清理自身容器和网络。验收 **1 failed、1 deselected、27.13 秒**。

GPT 明确说当前只确认“应用退出、store:25432 连接被拒绝”，没有确定数据服务停止、监听端口还是配置不一致；missing_evidence 包含数据库运行、审核配置及运行配置一致性。它提出合法数据库状态 action_id，并要求之后读取配置。实际 ATR `business_investigation.py` 在 gate=supported 时先 break，proposed_action 有值、executed_action=null。没有开展第二轮或访问数据库/配置，故不能作为自主 RCA 成功。

更早的原因在 `workflow.py` reviewer：机制日志和独立退出状态通过，但模型显式的 missing_evidence 被忽略，verification.missing_evidence 被覆盖为 []，从而生成 supported。真实模型没有编造 5432 或错误节点；这是本地确认规则忽略必要因果缺口的代码问题。证据为 `real-scope-fix/safe-analysis.json`、`real-scope-fix/real-isolated-trial.json`、`actual-docker-real-scope.xml`。

新增离线回归 3 项全部先失败：已核验启动症状但有必要缺口应 unresolved；有授权动作时应继续；无可执行动作时不得确认。最小修复在 reviewer 中合并、脱敏保留模型声明的必要缺口，缺口非空不得确认。Schema 明确这些是阻止确认的必要检查，没有自动编造填补缺口，也没有自动增加模型请求或工具权限。原有机制、预测、独立性、窗口、反证与图冲突检查全部保留。

首轮相关回归 76 通过、1 失败，仍是旧 12000 输入预算对新增文字的限制；精简重复说明而不提高预算。最终专项 **77 通过、85.49 秒**（`pending-contract-final.xml`），最新真实 Docker + Fake 严格真值与引用验收 **2 通过、55.91 秒**（`fake-pending-final.xml`），最新镜像 SSH/Docker Mock **75 通过、29.630 秒**（`acceptance-968b4e96`）。最终全量 **474 通过**后部署本地 API，随后开始第四次真实隔离复验。

对第三次真实 Run 保存的合法假设和实际证据进行纯 reviewer 回放，新代码返回 unresolved、mechanism_verified=true、passed=false、3 项必要缺口保留；原数据库记录未改，没有恢复或保存原始模型响应、再次调用模型或执行 SSH。结果 `real-scope-fix/offline-reviewer-replay.json`。

### 5.6 第四次真实隔离验收：严格拒绝再次出现的无效反证

Run `2641dadd-46d1-471d-a726-1bd46f88c208`，测试 Target `ed78310f-66fa-4a63-9b8e-ef3f5772a122`。第一轮 missing_evidence 被保留，状态 candidate；模型提出的 DB 状态检查实际执行，没有再次提前终止。第二轮在 `hypotheses[0].evidence_links[2].relation / counterevidence_target_mismatch` 安全停止，第三次工具之后没有执行更多工具。原始失败响应没有持久化，不能据此说出失败假设的具体目标 ID 或反证 ID。

两次供应商请求均有 actual 用量：第一轮 succeeded，输入/输出 **9100/630**，耗时 **19688 ms**、网络 **17016 ms**；第二轮 failed（结构引用校验），**9247/554**，耗时 **8922 ms**、网络 **6250 ms**。初始化 2422/2437 ms、重试 0；合计 **18347 输入、1184 输出**。3 个工具成功，严格 RCA **1 failed、1 deselected、37.29 秒**。恢复 HTTP 200 与 SELECT 1 成功，测试容器/网络清理。

本地反证校验和必要缺口规则按契约生效，但真实供应商仍会产生跨目标反证。这不是预算、超时或远程权限故障。证据 `real-pending-fix/safe-analysis.json`、`real-pending-fix/real-isolated-trial.json`、`actual-docker-real-pending.xml`。

### 5.7 一次有界语义纠正的隔离实验

为验证是否能解决上述供应商契约违反，新增仅在测试目录的实验包装器 `tests/integration/bounded_decision_experiment.py`。它不进入 API 镜像、不被正式调查导入；只有独立验收显式设定实验开关才安装。每个实验 Run 最多一次完整决策重新生成，最初仅允许 counterevidence_target_mismatch，第五次实验后增加 invalid_prediction；不纠正网络/HTTP 错误，不保留或重发失败响应，只提供安全错误路径、当前证据归属及合法预测候选。

每次请求仍经过原 reserve/settle 和完整 validator，使用同一个 Run 的 8 次/100000/20000/1500 限制，SDK 重试保持 0。非法决策不得执行工具；纠正仍非法就终止，原失败模型审计保持 failed，第二次请求具有新 request_attempt_id。免费专项 **6 通过、28.13 秒**，覆盖正确纠正、再次非法、模型次数用尽、HTTP 500/429/401 不纠正。真实 Docker + Fake 先验证故意注入错误及纠正链路；其后才考虑一次真实模型实验。

正式 ATR 没有启用自动纠正。任务书第五节要求“不得未经批准直接增加生产调查的付费自动重试”，操作者取消费用限制不等于批准新的生产自动纠正机制；任何正式启用须提供隔离实证后另行明确确认。

### 5.8 第五次真实实验与预测组合契约修复

隔离 Run `87c19639-2564-4561-b98c-fa98c794002b` 开启一次跨目标反证纠正实验，但第二轮在 `hypotheses[0].verification_predictions[0].value / invalid_prediction` 停止，因此纠正次数实际为 **0**。不能将开启开关说成纠正已经成功，也不能猜测未保存的非法预测值。

真实模型 **2 次**，actual 输入/输出分别 **9177/584、9293/557**，耗时 **32391/12078 ms**，总输入 **18470**、输出 **1141**。第一轮成功、第二轮结构引用校验失败；3 个工具成功，恢复 HTTP 200/SELECT 1 成功。严格验收 **1 failed、1 deselected、53.99 秒**，来源 `real-correction/safe-analysis.json`、`actual-docker-real-correction.xml`。

静态核对确认另一接口缺陷：原 JSON Schema 的预测 value 是 string/int/bool 宽联合，operator 也是宽联合；后端却按 field 限定状态枚举、HTTP 100–599、布尔值、日志长度/非通用信号及比较方式。离线 22 类组合中 **8 通过、14 失败**，证明输入 Schema 可接受后端必拒绝的值；这些合成值不是历史非法原值。

修复将工具、比较方式、值域汇总为 `verification.py` 的共同 PREDICTION_RULES，Prompt Schema 对每种 field 使用 oneOf 绑定精确组合。后端严格条件保持不变，Prompt 不再重复另一份手写规则。补充大写、Unicode casefold 和尾部换行一致性，总 **24 类组合**；联合规则及旧 12000 多轮压缩专项 **26 通过**。当时没有提高预算或继续减少证据预览宽度；该版本随后完成的免费完整验证见下文，通过后才发起下一次真实请求。

隔离纠正实验同时增加 invalid_prediction 的一次受限纠正，反馈中的合法值也来自共同规则。正式 API 仍没有自动纠正路径；实验代码只在测试目录。免费专项新版本 **7 通过、35.18 秒**；新规则真实 Docker + Fake 故障/正常验收 **2 通过、60.48 秒**，故意非法输出审计保留；最新镜像 SSH/Docker Mock **75 通过、31.479 秒**（`acceptance-e966b1a7`）。

最终全量回归 **504 通过、0 失败、735.42 秒**，其中 **491 项后端、13 项 Chrome 浏览器**（`full-prediction.xml`）。全量执行期间后端代码没有变动；隔离包装器随后增加的一项预测纠正测试包含在单独 7 项专项中，不将全量虚报为 505 项。只有上述无需付费验收通过后，才启动第六次隔离真实复验。

本地 API 再次安全重建，部署前六类活动计数均为 0。容器内 8 份相关源码与本机 SHA-256 一致，Settings 保持原模型、low、180 秒、20000/100000/1500、8/20；无网络拦截验证 Worker 180 秒、外层 190 秒、SDK 重试 0。42 张表、405 个资料及 `.env` 与重建前摘要完全一致，原 Volume 创建时间未变，API healthy，API/Web HTTP 200；证明保存于 `final-deployment-verification.json`。API 重建会清空内存 SSH 密码会话，隔离实验不需要生产密码。

### 5.9 第六次隔离实验：一次纠正不足以保证后续有效

Run `00005cf7-6dca-4f00-ade1-e2af99497af2`，测试 Target `4ee32b5e-47d8-486a-8560-a1638f6cd203`。6 次实际 GPT 请求中 4 次合法、2 次结构引用校验失败，6 次工具成功；用量输入 **60471**、输出 **5912**。第 4 次拒绝路径 `hypotheses[0].evidence_links[3].relation / counterevidence_target_mismatch`；隔离包装器唯一纠正生成了第 5 次合法决策，继续执行工具。第 6 次又在 `hypotheses[0].evidence_links[2].relation` 发生相同错误，纠正额度已用完，安全停止。

| 请求 | actual 输入/输出 | 总耗时 ms | 网络 ms | 状态 |
| --- | ---: | ---: | ---: | --- |
| 1 | 9376 / 950 | 40968 | 38281 | succeeded |
| 2 | 9608 / 1097 | 23078 | 20390 | succeeded |
| 3 | 10115 / 1093 | 25828 | 23172 | succeeded |
| 4 | 10201 / 1202 | 13484 | 10828 | failed / validate |
| 5 | 10695 / 864 | 37468 | 34828 | succeeded / 唯一实验纠正 |
| 6 | 10476 / 706 | 30485 | 27797 | failed / validate |

各次 SDK 重试均为 0；唯一语义纠正是新请求并计入相同 8 次/100000 预算，不能混称 SDK 自动重试。最终 unresolved / model_schema_error，严格 RCA **1 failed、1 deselected、180.40 秒**；隔离恢复 HTTP 200、SELECT 1 成功。原失败响应未保存，不猜测具体反证 ID。证据 `real-prediction-fix/safe-analysis.json`、`actual-docker-real-prediction.xml`。

随后离线明确复现组合约束缺口：有效节点引用及全局证据 ID 单独合法，并不代表它们可以组成 refutes；原 Prompt Schema 允许 3 类后端必拒绝的跨节点或无归属反证（6 例中 3 通过、3 失败，`scoped-refutes-schema-red.xml`）。最小修复在 Hypothesis 中按本轮精确 target_node_id 条件约束可作为反证的 evidence_id，仍允许依赖证据用于 supports。后端同节点严格校验、历史反证及证据门均未修改；不自动纠正关系或提高纠正次数。

另在无可用预测场景复现空 oneOf 不符合 JSON Schema（2 例均失败，`no-prediction-schema-red.xml`）。替换为显式禁止 Prediction 的 not:{}；假设仍可给空预测列表，缺少可核验预测仍不能确诊。

### 5.10 操作者授权的新预算

操作者要求提高预算、停止继续精简输入，并明确选择 **单次 30000、累计 200000**。`.env` 仅修改 `INVESTIGATION_INPUT_TOKENS=20000→30000`、`BUDGET_TOTAL_TOKENS=100000→200000`。模型仍为 gpt-6.1-sol，low、输出 1500、请求 180 秒/Worker 190 秒、模型 8 次、工具 20 次、总执行 1800 秒、零 SDK 重试及全部授权保持不变。Compose 解析、重建后的容器环境和实际 Settings 均已核对新值；不改变此前实验或历史 Run 的独立预算。

本次专项 **112 通过、0 失败、147.77 秒**（`scoped-refutes-final.xml`）。真实 Docker + Fake 正常/故障及故意非法决策纠正 **2 通过、63.62 秒**（`fake-scoped-schema.xml`），该镜像的隔离 Linux/OpenSSH、Docker Mock、异步与浏览器验收 **75 通过、32.744 秒**（`acceptance-339bbddd`）。Ruff 与差异空白检查通过。

新全量首轮 **515 通过、1 失败、744.53 秒**，失败为 `test_omitting_a_refuted_hypothesis_does_not_erase_its_counterevidence`。独立复现及只读查询证明第三轮估算 12028 超过该案例的旧 12000 上限，停止原因为 request_input_limit，原反证没有丢失。按操作者明确选择，将这一反证生命周期案例的新 Run 设置为 30000，保留全部原断言；历史反证与预算专项 **13 通过、15.61 秒**（`refutes-history-final.xml`）。生产代码没有因此继续改动。旧预算发送前拒绝与历史预算不变仍由独立测试验证。

再次完整回归覆盖全部 47 个测试文件，按此前耗时分成互不重叠的两组，清单 `full-regression-groups.json`。A 组 **158 通过、383.73 秒**，B 组 **358 通过、374.22 秒**；合计 **516 通过，0 失败/错误/跳过，503 项后端、13 项 Chrome**。已逐项核对与原 516 个案例集合完全相同，无遗漏和重复，证据 `full-final-verification.json`。此后才部署 30000/200000 并启动第七次真实隔离实验。

新契约的不可压缩内容在旧 12000 合成案例中超过上限，旧预算继续安全拒绝请求，不通过改写历史预算掩盖它。历史 Run 可以继续查询；若以历史独立预算恢复调查，新契约仍可能触发其原输入上限，不会自动升级历史快照。回归同时覆盖原 12000 的发送前拒绝、授权新 30000 的合法输出/完整引用、以及修改 Settings 后旧预算快照不变。原压缩算法保留，未继续收窄日志预览或删减模型输入。

### 5.11 本轮精确代码位置

- `backend/app/agent/model_schema.py:19`：候选全集及 30 节点容量；`:110`：Schema 同源候选；`:129`：反证按目标绑定；`:155`：预测组合；`:184`：严格决策校验；`:198`：跨节点反证拒绝。
- `backend/app/agent/verification.py:22`：预测共同规则；`:32`：工具/字段/操作/值域严格校验。
- `backend/app/agent/workflow.py:157`：合法节点及证据输入；`:176`：精确引用 Prompt；`:336`：必要证据缺口合并；`:340`：缺口不得确诊。
- `backend/app/context/budget.py:51`：创建时从 Settings 取新预算并持久化快照；`:100`：预算预留检查仍执行。
- `tests/integration/final_e2e_acceptance.py:374`：隔离真实实验读取原模型及已授权预算配置；正式模型 Worker 接口未改。

### 5.12 第七次隔离实验：实际用量超过预留预算

Run `e5288a92-0340-464d-b756-4589ee54bc98`，独立 Target `b81a0070-bffe-43de-9dd6-9bed00b7fbc6`。采用已授权新预算 30000/200000。一次真实模型调用解析及严格决策校验成功，没有使用实验纠正；本地输入估算 **11584**，预留输出 **1500**，预留合计 **13084**。供应商原始响应 usage 报告输入 **27809**、输出 **2620**，合计 **30429**。耗时 **37391 ms**，网络 **34750 ms**、初始化 **2390 ms**，SDK 重试 0，成功 HTTP 状态未保存在审计中。

`ContextBudgetManager.settle` 按既有规则发现 `30429 > 13084`，设置 `provider_usage_exceeded_reservation`；后续工具在 `remaining_seconds` / `consume_tool` 阶段拒绝。输入实际值没有超过 30000，累计也没有超过 200000，不能归因为新预算没有生效。两次基础工具成功，第三次工具仅创建失败审计，未执行 SSH，tool_calls 保持 2，读取 334 bytes，reserved_tokens=0。最终 unresolved，严格 RCA **1 failed、1 deselected、45.42 秒**；隔离恢复 HTTP 200、数据库 SELECT 1 成功，测试资源清理完成。

已证实：上述数字来自 Worker 对原始 HTTP usage 的严格整数读取，不是 LiteLLM 缺失用量补值；供应商报告计量明显不同于本地字节估算，并报告输出高于请求的 1500。尚未证实：供应商为何增加计量、是否包含隐藏上下文或不同推理计量、是否正确遵守输出参数。现有审计未保存相应细分，也没有原始请求体抓取，不能据此认定其计量错误或 LiteLLM 自动追加内容。第七次没有多轮有效输出，不能用来证明最新反证 Schema 已解决真实模型稳定性。

免费回放又复现摘要误分类：数据库明确是 `provider_usage_exceeded_reservation`，调查却显示 `tool_budget_exhausted`。新增真实编排 Fake 用量案例先失败，再仅修正停止原因及中文说明。供应商异常用量保护、实际计费记录、模型调用成功状态、工具阻断及人工预算审核要求保持原样。没有修改预算算法，没有自动清除异常标记，没有调用更多真实请求来等待偶然通过。既有第七次 Run 的错误摘要保留原始记录；修复作用于后续执行，不回写历史。

证据：`real-scoped-schema/safe-analysis.json`、`real-scoped-schema/budget-stop-evidence.json`、`actual-docker-real-scoped-schema.xml`、`provider-budget-stop-red.xml`。代码：`backend/app/context/budget.py:219`、`:227`（原保护），`backend/app/agent/business_investigation.py:311`（分类修复）。

### 5.13 最终版本复核与部署

供应商用量停止分类专项 **67 通过、0 失败、168.19 秒**（`provider-budget-final.xml`）。最新镜像 SSH/私钥/密码、Docker Mock、异步、浏览器验证 **75 通过、0 失败/错误/跳过、31.947 秒**（`test-results/real-discovery-ssh/acceptance-2d6fe8de/junit.xml`）。真实 Docker + Fake 正常/故障与一次故意非法输出实验 **2 通过、58.65 秒**（`fake-provider-stop.xml`）；故障模型为 Fake，结果 supported，正常 unresolved，均恢复 HTTP 200，不能称为真实 GPT 确诊。TypeScript/前端生产构建、Ruff 均通过。

最终完整回归覆盖全部 47 个文件 **517 通过、0 失败/错误/跳过**，包含 **504 项后端、13 项 Chrome 浏览器**。A 组 158 项、388.19 秒；B 组 359 项、381.54 秒。逐项核对：原 516 项均保留、新增供应商异常分类案例 1 项、无重复，与当前完整收集一致。证据 `full-provider-A.xml`、`full-provider-B.xml`、`full-provider-verification.json`。

全部免费验收通过后，检查六类活动计数和 `/proc` 中模型 Worker 均为 0；仅执行 `docker compose -p atr-runtime --env-file .env -f compose.yaml up -d --no-deps --force-recreate api`，未重建 Web、未执行 down。最新 API 镜像摘要为 `sha256:42eb95d1d2dcd44ea387f3952dc1f1bd5bc1fed066dda85fe6dcf5b385a1de76`。

最终 `.env`、Compose 解析值、容器环境和 Settings 均为输入 **30000**、累计 **200000**、请求 **180 秒**、输出 **1500**、模型 **8**、工具 **20**、low。无网络拦截验证 Worker 180 秒、外层硬超时 190 秒、SDK 重试 0；实际预算创建方法以纯内存事务截获，验证新快照 30000/200000/8/20/500000 bytes/1800 秒，没有向正式数据库创建 Run 或预算。

配置位置为 `E:\ATR\.env` 的 `INVESTIGATION_INPUT_TOKENS` 与 `BUDGET_TOTAL_TOKENS`；Compose 将其注入 API，`backend/app/settings.py` 读取，`backend/app/context/budget.py:51` 在 Run 创建时生成独立快照。容器必须重新创建才读取新环境。历史 Run 仍保持原预算。

API healthy、API 健康入口 HTTP 200、Web HTTP 200，8 份容器源码 SHA-256 与本地相同。重建前后 42 张表的行数及内容摘要完全相同，4 个 Target、随想记画像、人工映射、历史模型/工具审计及证据保留，405 份资料摘要一致，原 Volume 创建时间不变。新预算之外 `.env` 未再修改。测试标签下容器和网络均为 0。最终验证：`final-deployment-verification.json`、`final-health-verification.json`；API 重建使内存 SSH 密码会话清空。

## 6. 生产只读验收

授权目标固定为 `d997a5d5-8898-4f99-bbe7-765e38facac9`（suixiangji安全网关2，forced_gateway）。部署后仍有 4 个 SSH Target；API、DB 人工映射与 `/opt/suixiangji/atr-reviewed-config` 保持原样。

操作者刷新正常页面密码并通过有效期、预算、目标及映射核对后，生产新 Run：`46cf14d7-cd25-4f36-b97c-7b3198b4fae2`，事件 `14732197-62c8-4f08-ab17-dc16d4320f6f`，任务 `87b1d7c4-98ff-4f3b-b3da-f5b4cac096bc`，额度使用 1/2。北京时间 01:30:05 开始、01:33:41 结束，任务 completed，调查 unresolved。调查目标是如实检查当前健康与部署一致性，没有事先假定故障。7 次真实模型请求全部成功，第 8 轮在发送前因累计预算停止；没有自动创建第二个生产 Run。

部署曾导致内存密码会话清空，接口 required、目标不可调查，未回退其他目标。全部无需付费测试通过后，操作者经 ATR 正常页面重新提交，接口 ready，正式开始前剩余会话 1725 秒、API/DB 映射有效。未在聊天、Windows 缓存或浏览器存储获取密码。模型最长等待、工具最坏耗时与其它处理叠加时仍可能接近 30 分钟，因此凭据持续有效并无绝对保证，失效必须安全停止。

操作者另明确批准一次额外隔离真实 GPT 复验（单 Run 最多 8 次模型请求、100000 累计预算、180 秒、零重试），在本次生产 Run 结束后执行；不增加生产 Run 上限。

### 6.1 实际模型调用与停止原因

| 请求 | actual 输入/输出 | 总耗时 ms | 网络 ms | 初始化 ms | 状态 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1 | 9614 / 294 | 20437 | 16704 | 3593 | succeeded |
| 2 | 9607 / 842 | 36115 | 32947 | 3027 | succeeded |
| 3 | 11258 / 905 | 57510 | 53724 | 3621 | succeeded |
| 4 | 11715 / 906 | 17582 | 13802 | 3611 | succeeded |
| 5 | 11691 / 184 | 14292 | 11130 | 3018 | succeeded |
| 6 | 11758 / 191 | 8967 | 5803 | 3023 | succeeded |
| 7 | 11785 / 973 | 49034 | 45863 | 3027 | succeeded |

供应商实际输入合计 **77428**、输出 **4295**，累计 **81723**；费用未知，缓存用量没有独立审计字段。7 次解析耗时均小于 1 ms，重试均为 0；没有 Timeout、HTTP 错误或结构引用错误。成功调用的 HTTP 状态未保存，不能直接标为 HTTP 200。

第 8 轮安全诊断为 `BudgetExceeded / total_token_limit / stage=budget`，输入估算 **17655**，未超过单次 20000；但 `81723 + 17655 + 1500 = 100878 > 100000`，故累计预算预检查拒绝。该轮没有模型请求记录、没有付费调用；calls 保持 7，reserved_tokens 归零。证据与前七轮报告保留，Run 的 model_failed 表示后续准备阶段失败，不能解读为前七次供应商请求失败。代码位置：`backend/app/context/budget.py:115`，`backend/app/agent/business_investigation.py:423`、`:574`。

### 6.2 实际工具、证据与多轮决策

9 次工具全部 succeeded、全部实际绑定指定 target_id，读取 **33519 bytes**、保存 **9 条证据**。API 状态/日志为最初基础采集，之后七个动作由真实模型选择。下表只列编号和范围，原始日志保留在原 SQLite，不复制到报告。

| 工具顺序 | 实际读取 | 证据 ID |
| --- | --- | --- |
| 1 | API container.status，running | `1fa9e186-247b-410f-9c2f-90f1f5e5f649` |
| 2 | API container.logs，有界历史日志 | `a1647af0-a428-4c6e-842c-74da5d8b3975` |
| 3 | DB container.status，running | `5e5aad3b-cc3e-4396-b304-13461dd7f66b` |
| 4 | 授权目录 config.list | `38cab7df-9879-419b-ba0f-0f888d57974e` |
| 5 | deployment.reviewed.json | `2d334f5f-88cc-405c-99a5-6a3837a2cea9` |
| 6 | api-runtime.reviewed.json | `d523d635-24b3-45b7-94a3-c94edf0a71e8` |
| 7 | compose-staging.reviewed.json | `5bd6b6a6-518b-4b0c-a59e-008d4ee27140` |
| 8 | DB container.logs，有界历史日志 | `5ed92daf-b75d-4986-a3ea-645a6011a6c7` |
| 9 | postgres-config.reviewed.json | `e7c7a84e-e632-463d-9f0f-c4eec40dc838` |

全部配置路径位于 `/opt/suixiangji/atr-reviewed-config`；读取文件这一动作是当前 SSH 观测，文件内容仍是管理员审核的静态快照，不能证明当前数据库连接或运行参数。日志采集时间为当前时间，日志中事件可以更早，不能把采集时间替代事件时间。

| 推理轮 | 新动作 | 假设与证据变化 |
| --- | --- | --- |
| 1 | DB 状态 | 不编造故障，要求先获得依赖状态。 |
| 2 | 配置清单 | API 配置不一致候选 0.28、DB 不可用候选 0.20；当前 DB running 作为停机方向反证，门控未确认。 |
| 3 | 部署快照 | 新目录证据改变检查方向；配置候选 0.32，仍需期望/实际配置对照。 |
| 4 | API 快照 | 新快照引出待核对的绑定/入口配置可能性，置信度 0.34；仍要求实际可达性，门控未确认。 |
| 5 | Compose 快照 | 没有给出新根因，选择进一步核对依赖声明。 |
| 6 | DB 日志 | 没有给出新根因，选择独立依赖日志检查。 |
| 7 | PostgreSQL 快照 | API 配置候选降低到 0.20、DB 不可用到 0.15；明确连接、就绪、监听和完整配置对照仍缺失。 |
| 8 | 无 | 累计预算预检查停止，保留已有假设和反证。 |

最终结论是**没有已证实生产故障**，并非业务完全健康。真实模型有根据新证据选择动作、调整假设及保留不确定性；其部分支持/反证关系较粗，同一证据存在不同关系，严格证据门均拒绝确认。不能把这个健康检查 unresolved 报告作为生产 RCA 成功。

完整安全字段投影保存为 `test-results/final-e2e-20261012-8336e93a/production-run-safe-audit.json`；包含模型阶段/时间/用量、实际目标、工具与证据 ID、各轮门控和安全错误，没有完整 Prompt、密钥或完整生产日志。

## 7. 数据保留证明

开始、部署前和部署后只读计算 SQLite 42 张表的行数及逻辑内容 SHA-256；部署后的全部表与基线一致。正式数据库仍是原 `/data/runtime.sqlite3`；Volume `atr-runtime_runtime-data` 创建时间仍为 `2026-10-10T01:44:39Z`。已有 12 个历史 Run、4 个 SSH Target、业务画像版本、审核记录、映射、上传资料、模型审计和证据均未修改。

前六次实验及部署 `.env` SHA-256 为 `4A1B51C0720186C794FFB8363ED2CFC67CF4FF2C273629F6732BDC898FCEB41A`。操作者随后批准两项预算调整，新摘要为 `02F7F528CE82DAB6DC95393F3506A98E0DBBE87F37B3E7CA2957ACCBB5992B72`；替换这两行值为占位后的其他字段摘要完全相同，密钥、Base URL、模型及其余配置字节未变，证据 `budget-configuration-change.json`。后续授权真实 Run 只允许正常追加自己的运行、证据、审计和图快照；不得将追加记录说成整个数据库内容完全不变。

最终 API 部署前后及生产验收后，上传资料 405 个文件、3522895 bytes，文件清单及内容的联合摘要均为 `0e9a8af16a30eb17a37a3b43ad4e75674f9d2ff2e638a84aa613a007d9f44383`，完全一致。

生产验收后逐条比对原有记录摘要：12 个历史 Run、10 个历史事件、33 个工具审计、19 个模型审计、28 条证据、41 条假设、20 个步骤、20 条业务审核、5 个画像版本、1 个上传记录及 28 条映射均未变化或丢失。新的生产 Run 正常追加 1 个事件/Run、9 个工具/证据、7 个模型审计、8 个步骤/假设。原有人工决定全部保持原值；4 个 SSH Target 仍在，指定目标的模式和配置授权目录符合预期。

2 个 Target 行摘要在操作者补充密码后变化：`6ee8be58…` 和 `d997a5d5…`。API 访问记录分别有正常 `/credential` POST 200；`backend/app/connectors/service.py:78` 的既有密码提交逻辑更新凭据引用，密码只在后端内存。没有保存原始凭据引用用于对比，故不宣称这两行字节完全未变。旧目标没有用于本轮任何工具。其它 2 个 Target 行摘要未变。安全保留证明：`data-preservation-after-production.json`。在后续回归期间操作者多次经正常页面提交 d997… 密码，API 访问记录为 /credential POST 200，仅该 Target 行摘要再次变化，其余历史记录逐条摘要不变。最终部署使用最近正常提交后的基线，不将用户操作误报为部署改写；部署前后全部 42 张表一致。

## 8. A / B / C 独立判定与剩余限制

| 验收 | 当前判定 | 依据 |
| --- | --- | --- |
| A 工程端到端 | **通过**，包括生产只读链路 | 指定目标 9 次真实 SSH 工具成功、7 次真实模型成功；预算检查在发送前安全停止，审计/证据/状态一致。 |
| B 真实 AI 多轮自主诊断 | **多轮自主性及受限候选诊断通过**；稳定确诊能力仍未证明 | 生产根据新证据选动作、降低假设置信度并如实保留缺口；隔离模型自行识别端口差异。证据关系仍有错误，不能称为可靠最终 RCA。 |
| C 实际故障 RCA | **真实模型隔离确诊未通过**；生产无已证实故障 | 第 1/2 次 unresolved，第 3 次提前 supported 仅确认症状，第 4/5/6 次严格拒绝非法决策，第 7 次供应商用量异常安全停止；一次实验纠正不能保证后续始终有效。恢复正常仅证明真值可复现，不能替代模型严格确认。 |

剩余技术限制：供应商仍可能违反 Prompt 契约；压缩可能省略重要文本，不能把省略内容当证据；未支持的机制只能作为候选；单次输入与累计预算、180 秒请求超时仍可能终止调查；密码会话约 1800 秒，最长多轮请求可能消耗大部分有效期。证据不足时 unresolved 是安全结果，不是已定位根因。

未修改生产服务、网关策略或权限；未执行生产故障注入、自动修复、Push、Merge 或 PR。

## 9. 真实调用额度与费用

目前共 **33 次真实请求**：首次隔离 6、生产只读 7、第二次隔离 8、第三次隔离 1、第四次隔离 2、第五次隔离 2、第六次隔离 6、第七次隔离 1。共 7 个隔离 Run、1 个生产 Run；生产使用 1/2 个新 Run，第二个未启动。累计供应商报告输入 **348154**、输出 **27621**，合计 **375775 Tokens**。有实际模型响应但未通过引用校验的调用，不能记为成功决策。费率未配置，**费用未知**；缓存及推理 Token 细分未知。SDK 重试全部为 0，隔离仅有第六次实验的 1 次明确语义纠正，无第 8 个生产请求，供应商内部行为不可观测。操作者已取消本轮费用限制，新预算经明确批准为 30000/200000；这不取消 8 次、20 次、180 秒限制和供应商用量异常保护，也不授权生产自动语义纠正。

## 10. 最小剩余工作与下一步

目前未达到完整 A/B/C 全部通过。已依据实际决策修复引用候选、反证组合、预测组合、忽略必要缺口而提前终止及错误分类问题，并完成最后版本 517 项全量回归、隔离 Docker/SSH 验收和本地部署。七个失败的真实隔离案例全部保留，不被后续结果覆盖。当前外部阻断为供应商实际 usage 与请求预留明显不一致；单纯提高单次/累计上限不会解除 `provider_usage_exceeded_reservation`。最新契约仍需要有效多轮真实输出才能继续验收，不能因这次第一轮合法就宣称稳定问题已解决。

若改动假设契约，应给不同因果主张明确且有界的身份与反证解释规则；不允许模型换一个名称就丢弃已有反证，也不允许自动把 DB running 的关系从 refutes 改成 supports。必须补充“原停机方向被反驳、端点方向获得新证据、未解释反证不能隐身”的回归，先通过离线完整调查再部署。这个语义治理涉及证据门与生命周期，不能为通过验收仓促删除反证。

生产累计预算停止另有独立原因。按操作者指示，不继续精简输入；新建 Run 的预算已部署为 30000/200000，剩余累计额度的请求预留检查保留。仍不得取消预检查或把静态快照和运行状态当连接成功证明。后续应先核验中转服务实际计量与输出参数契约，再决定是否需要经明确审核调整估算/预留策略；本轮没有以未经验证的计量比例自动提高预留，也没有关闭保护。

下一轮生产调查由操作者手动执行以下步骤，本轮不再自动创建生产 Run：

1. 选择 suixiangji → suixiangji安全网关2，核对完整 Target 为 `d997a5d5-8898-4f99-bbe7-765e38facac9`、模式 forced_gateway、配置目录 `/opt/suixiangji/atr-reviewed-config`。
2. 经正常页面重新提交 SSH 会话密码。最后一次 API 更新会清空内存会话；不要从聊天提供密码。
3. 核对 API、DB 两个人工映射仍有效、对象正确且属于同一 Target。若已过期，由操作者根据真实范围审核；本轮没有自动延长授权或改变确认决定。
4. 创建新的只读调查，提出实际症状或健康核查目标；新 Run 预算应为输入 30000、累计 200000，模型 8、工具 20、输出 1500、low、180 秒。恢复历史 Run 继续使用其原独立快照，不会自动获得新额度。
5. 若再次显示供应商用量超过预留预算，保留 Run/attempt ID、估算与 actual usage 并核验中转服务契约；不要反复新建调查等待偶然成功，也不要把预算异常当成服务器故障。

不要把本轮健康检查 unresolved 当故障。生产故障注入和扩大权限仍须另行明确批准；隔离的一次语义纠正实验也没有启用到生产。当前尚不推荐把未完成的真实隔离 RCA 当成正式验收通过。

报告证据目录：`E:\ATR\test-results\final-e2e-20261012-8336e93a`。API/Web 正常、原历史与资料保留；本地修改尚未提交或同步 GitHub。
