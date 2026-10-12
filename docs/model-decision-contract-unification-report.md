# ATR V2.2 模型决策契约统一检查与修复

日期：2026-10-12，北京时间。本轮只读查询已有真实记录，使用 Fake Provider 和隔离临时数据库测试；真实 GPT 调用、正式 Run 创建和生产 SSH 执行均为 0。随后操作者明确授权将当前进度推送至 GitHub main，不改变其余执行边界。

## 本次实际失败

| 项目 | 可核验证据 |
| --- | --- |
| Run | `bb882ea5-cd94-413b-bdd9-4553cfab99c7` |
| SSH Target | `d997a5d5-8898-4f99-bbe7-765e38facac9` |
| 图快照 / 画像版本 | `55bcef27-f880-4949-a9f6-eafb57978b48` / 5 |
| 执行时间 | 08:41:12—08:41:49 |
| 模型记录 | `d971bc4f-e444-42bc-b602-fb64bb37ab66` |
| 失败 | `ModelDecisionError / model_schema_error / validate` |
| 安全错误路径 | `proposals[0].next_tools / validation_error` |
| 用量 | 供应商 actual：输入 7826、输出 1075；本地输入估算 7692 |
| 耗时 | 总 29387.887 ms、网络 25681.616 ms、初始化 3549.692 ms；零重试 |
| 预算 | 输入 30000、累计 200000、8 次模型、20 次工具；预算 stop_reason 为 null |
| 真实工具 | host.info、host.memory、host.disk、network.listeners、service.list、container.list、process.list，7 次全部成功、7 条基础证据 |

这次处于**运行对象发现阶段**，输出是 MappingReport 的 proposals；尚未进入输出 hypotheses 的正式调查决策。调用链：business_investigation → discover_for_run → call_json → 独立模型 Worker → JSON 解析 → validate_mapping。已有审计的栈精确指向旧 `discovery.py:57` 的 next_tools 集合检查；预算、超时和远程权限并非此次拒绝原因。

`next_tools` 的实际契约为**必须提供的字符串数组，最多 6 项**，每项是建议工具名称。service 对象只允许 service.list/status/logs；container 只允许 container.list/status/logs；process 只允许 process.list，且必须有业务身份依据。它不接受 Action ID、工具对象、参数或 Shell。这些建议不能执行工具或授予权限，仍需人工审核映射。

原输入 Schema 只有 list[string]，没有以上动态枚举和组合规则，因而允许模型生成后端必拒绝的工具名称。抛出的 unknown_action 又不在安全诊断类型白名单中，被降级为 validation_error。现有记录能证明：至少一项工具不属于所选对象类型允许的集合；不能证明具体工具名称、对象类型或完整 proposal。

**结论：存在输入 Schema 与后端语义契约不一致。模型返回值违反后端已有规则，但该限制没有完整提供给模型。原始响应未持久化，本轮没有恢复、打印或保存原始非法值。** 用历史脱敏清单重建的候选池包含 7 个 service、3 个 container、1 个无身份依据的 process；仅 10 个对象可作为有效映射候选。离线合成 proposal 使用 config.read 复现旧 Schema 通过、严格校验拒绝，新 Schema 拒绝；这只是合成案例，绝非本次原始工具值。

## 完整契约审计与统一

发现和正式调查保留各自原报告格式，复用引用与角色约束生成函数；未删除原有字段。

| 字段或组合 | 统一规则与处理 |
| --- | --- |
| proposals.service / role | 来自同一实际业务服务映射，Schema 联合约束，Validator 精确匹配；角色失败定位到 role。 |
| candidate_key / kind | 只允许当前有效候选；绑定实际类型；无业务身份依据的 PID 不可作为映射 proposal。 |
| proposals.evidence_ids | 仅引用所选候选自己的已采集证据，保留 1—8 项约束。 |
| proposals.next_tools | 按候选类型、固定注册表和当前 Docker 读取能力生成允许集合，Schema 与 Validator 共用 mapping_tools；受限 Docker list 不宣称具备 status/logs。 |
| proposals 重复组合 | 同一 service/candidate_key 最多出现一次，即使解释文本不同也不得重复；Schema contains/maxContains 与后端一致。 |
| hypotheses.target_node_id / target_role | 来自当前 Run 同一 service 节点集合，节点与角色联合约束；保留 30 个节点容量及超限安全停止。 |
| evidence_links | 全部 ID 必须存在；refutes 必须归属假设的精确目标，依赖 supports 仍由原证据门核验相关性。 |
| verification_predictions | 节点、工具、字段、运算符、值域、数组容量同源；工具必须是当前候选或已有观测能够提供的检查。 |
| JSON integer 状态码 | Schema 合法的 200.0、100.0、599.0 精确归一为整数；仍使用原 100—599 范围。字符串、布尔、200.5、越界值继续拒绝，其他字段与 ID 不进行转换。 |
| adaptive action_id | 必须来自当前实际提供给模型的最多 12 个候选；Schema、上下文与 Validator 使用相同集合。 |
| legacy tool_id / target_node_id | 按实际动作对绑定，不能从两个独立枚举拼出不存在的组合。 |
| 所有结构字段 | 延续 Pydantic 必填、严格类型、枚举、数组/长度/数值限制和 extra=forbid；原可选 null/空数组兼容规则保留。 |

发现输入因预算缩减候选时，每次重新生成 Schema，再把同一最终上下文传给 Validator。原始基础观测、发现记录和证据不删除；已有服务/候选容量保持不变。正式调查文本压缩仍保留合法引用集合，不降低预算检查，不继续删除业务上下文。

预测数值修复解决另一项经测试确认的契约差异：标准 JSON Schema 把无小数部分的数值记法作为 integer，而严格 Python int 原本拒绝 float。归一只对状态码的合法整数数学值进行，结果仍是 int，再经过全部原语义、引用和证据校验；没有猜测 ID、改变证据关系或执行非法动作。

新 next_tools 拒绝使用原有安全 action_not_allowed 类型，页面显示准确路径和类型，原始内容不回显。历史记录中的 validation_error 保持原样，不回写数据库。

## 修改文件

- `backend/app/agent/discovery.py`：MappingReport 动态契约、候选/工具/角色/证据组合、重复 proposal、裁减后同步 Schema、准确安全错误类型。
- `backend/app/agent/model_schema.py`：共享引用/角色规则、节点角色组合、预测可观测工具、JSON integer 合法记法、旧模式动作组合。
- `backend/app/agent/workflow.py`：模型输入、Schema 和 Validator 共用实际提供的动作池。
- `tests/test_decision_contract_consistency.py`：整个嵌套结构与跨字段矩阵，以及本地 Fake HTTP/独立 Worker 的发现→人工审核→恢复→多轮调查。
- `tests/e2e/test_model_errors.py`：浏览器验证 proposals[0].next_tools 的安全诊断。
- `tests/test_v22_adaptive.py`：同时验证旧 12000 快照安全停止、当前 30000 预算下的原反证保留和依赖调查。
- `tests/integration/test_bounded_decision_experiment.py`：隔离实验夹具的上下文与校验状态使用同一工具集合；正式调查仍没有自动纠错。

本轮开始前的未提交修改完整保留，检查点位于 `test-results/2026-10-12-contract/pre-existing-changes`。同步 GitHub 时同时包含此前相关的引用、安全错误治理、验收报告和隔离测试；不包含本地凭据、数据库或测试运行文件。

## 验证结果

- 初始完整契约复现：14 失败、10 通过，明确暴露动态枚举、角色、动作组合、重复 proposal 及错误分类缺口。
- JSON integer 独立复现：3 个 Schema 合法整数记法被旧 Pydantic 拒绝，6 个边界案例符合预期。
- 最新字段/引用/预测专项：112 通过；完整 Fake 发现→审核→恢复流程 4 次模型决策、11 次工具均成功，最后保留未证实项。
- 浏览器 next_tools 诊断案例通过；不回显非法值。
- 原 12000 多轮案例曾因估算 12299/12007 被安全拦截；去重相同节点条件后一个恢复，另一仍为 12037。测试明确覆盖旧预算安全停止与当前 30000 正常完成，不修改实际配置或历史预算。
- 初版完整回归：570 通过、1 失败，原因是隔离纠正夹具只给 Prompt 提供工具却未给 Validator 提供；修正夹具后专项 7 通过，正式校验未放宽。
- 最终完整回归：48 个测试文件、581 项全部通过（567 后端、14 浏览器），失败、错误、跳过均为 0；与收集清单逐项对照，缺失、额外及重复均为 0。
- 最终 Linux/OpenSSH 密码与密钥、Docker 只读 Mock、异步任务及浏览器隔离验收：75 项全部通过，30.667 秒。
- 最终本地 Docker＋Fake Provider：2 项通过，55.44 秒，覆盖已知端口故障、正常配置及恢复；未开启自动纠错，未使用真实 GPT 或生产资源。
- 前端 TypeScript 与生产构建、相关文件 Ruff 检查通过。

证据目录：`E:\ATR\test-results\2026-10-12-contract`。关键文件：latest-run-safe-audit.json、historical-context-safe-replay.json、contract-red.xml、json-integer-red.xml、integer-contract-final.xml、browser-discovery-error.xml、full-final-collection.json、full-final-verification.json、docker-fake-final.xml、final-deployment-verification.json。最终 SSH/Docker 验收位于 `test-results/real-discovery-ssh/acceptance-44af866e/junit.xml`。

## 安全与剩余边界

没有自动补齐未知 ID、创建图节点、扩大工具集合、修改人工审核或把推断当证据。输出通过结构契约仍不代表根因成立：时间窗口、独立观测、运行机制、反证、图冲突和缺失证据由原 Evidence Gate 决定。

本次审计发现的业务引用、组合、候选状态和 JSON integer 差异均有回归覆盖。标准 Schema 与 Prompt 无法保证供应商始终遵守，也不能表达真实因果关系或远程授权在请求后撤销；后端引用、Policy、Run 绑定、人审和证据门继续作为执行边界。真实模型稳定性本轮未验证，不以 Fake 结果声称真实 RCA 已通过。

模型、Base URL、密钥、推理 low、180/190 秒、30000/200000、输出 1500、8 次模型/20 次工具和零 SDK 重试保持不变。本轮不创建正式 Run、不连接生产服务器。API 更新会清空短期 SSH 密码会话，下一次手动调查需在正常页面补充，并核对映射有效期。

## 最终部署与 GitHub 同步

确认排队、运行中调查、模型请求、工具调用、发现任务与独立模型 Worker 均为零后，仅重新创建本地 API；未执行 down，未重建 Web 或删除 Volume。

API 容器 healthy，API `/api/v1/health` 和 Web 均返回 HTTP 200。容器内修改后的三个契约文件与本地 SHA-256 完全一致；独立 Worker、预算、设置及证据校验代码也与本地一致。无网络模拟确认 Worker 接收 180 秒超时、外层硬超时 190 秒、重试 0。

`.env` 摘要保持不变，Compose、容器环境和 Settings 均为单次 30000、累计 200000、输出 1500、模型 8 次、工具 20 次、推理 low、请求 180 秒。模型与调用协议保持 gpt-6.1-sol / chat_completions。

重建后只读核验：SQLite 全部 42 张表的行数与内容摘要与本轮开始时及重建前完全一致；4 个 SSH Target、随想记画像、人工映射、历史 Run、模型审计及证据保留。405 份资料共 3522895 字节，其内容摘要一致。`atr-runtime_runtime-data` 仍为原卷（创建时间 2026-10-10T01:44:39Z）；目标 d997a5d5… 仍为 forced_gateway，授权路径仍为 `/opt/suixiangji/atr-reviewed-config`。隔离验收容器和网络已经清理。

API 重建清空内存 SSH 密码会话。下一次真实验收由操作者在正常页面重新提交会话密码，选择 suixiangji安全网关2，核对有效 API/DB 映射后手动启动；本轮没有创建正式 Run、调用付费模型或连接生产服务器。

GitHub：按操作者后续明确授权，将本次修复、对应测试及此前相关验收进度直接提交并普通推送 main；不强推、不创建 PR。上传前检查配置密钥和私钥，未发现泄露；本地凭据、数据库、资料、测试运行证据及 `.env` 不上传。实际提交标识以 Git 历史为准。
