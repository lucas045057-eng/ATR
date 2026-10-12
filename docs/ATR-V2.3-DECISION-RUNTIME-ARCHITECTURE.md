# ATR V2.3 决策与 Runtime 架构

## 目标

V2.2 让模型一次输出假设、证据关系、验证预测和工具参数，模型输出结构与后端校验规则容易分叉。V2.3 将模型职责收缩为有限的调查判断；Runtime 负责解析当前合法节点和动作、选择已授权动作、构造可核验事实并决定调查是否可以结束。模型不能创建工具调用，也不能自行建立证据关系或确认根因。

## 新 Run 的协议

`AGENT_DECISION_PROTOCOL=minimal_v2` 是新自适应调查的配置。设置快照写入 Run 的模型配置；暂停后恢复继续使用该快照。可解析的旧 Run 快照若没有协议字段，则按 `legacy_v22` 读取，历史 Run 不会被改写；损坏的快照或明确的未知协议会失败关闭，不静默切回旧流程。非自适应旧流程也继续使用 V2.2。当前部署配置明确选择 `minimal_v2`，Settings 的代码默认值仍为 `legacy_v22`，以便未配置时安全兼容。

模型只返回：

- 简短摘要；
- 最多 3 个候选假设（当前服务图节点 ID、已登记故障机制、理由、待补信息、置信度）；
- 下一步调查说明；
- 一个 Runtime 提供的精确 `action_id` 或空值；
- 是否建议结束及理由。

Schema 在每轮从当前 Run 的服务节点、故障机制与动作候选动态生成。输出中不含证据关系、验证预测、动作参数、SSH Target ID 或可执行命令。模型上下文中的动作只含动作 ID、工具类型和服务标签；Runtime 绑定表和动作目标参数不再进入模型上下文。当前证据仍按既有策略提供给模型用于推理。继续调查时必须选择当前候选中的确切动作 ID；未知节点或动作按致命决策错误拒绝。

## 调查流程

1. Run 固定业务图、目标 SSH Target 与授权范围；当前 Runtime 只读候选在模型调用前由现有规划器生成，并受原有策略过滤。
2. 若没有新动作候选，Runtime 不调用模型，直接用已有证据重算结果并结束。
3. 模型给出紧凑决策；`adapt_minimal_decision` 校验输出，精确解析动作 ID。非关键假设格式或机制错误可单项隔离并写入安全审计；未知目标、未知动作、越权动作及协议错误停止本轮，不执行任何工具。
4. Runtime 从当前、未过期且位于事件窗口的状态/日志证据构造事实。日志原文不复制进 Fact；只记录解析出的服务状态、故障信号、登记机制、来源、时间、新鲜度和证据引用。
5. Runtime 按同一服务目标关联 Fact 与候选 Claim；跨服务事实不会被拿来支持或反驳另一服务的故障假设。观察中同时存在健康与失败状态时，Claim 保持 `unverified`，不被单条旧健康状态误标为已反驳。证据关系由 Runtime 生成并交给原 Evidence Gate，Gate 校验逻辑没有放宽。
6. `classify_outcome` 只在原 Evidence Gate 已支持时报告 `root_cause_confirmed`。已发现服务退出但 Gate 未确认因果关系时，报告 `direct_fault_cause_unknown`。仅完整授权范围均有当前健康状态、且无故障日志信号时，报告 `no_fault_observed_in_scope`；其余报告 `evidence_insufficient`。
7. 模型的结束建议不是结论。Gate 已支持、检查耗尽或完整范围内未观察到故障时可结束；若还有未验证范围/直接故障且仍有授权候选，Runtime 可选择评分最高的未执行只读动作，并在审计中记录覆盖。Runtime 不会把不完整证据描述为健康，也不会超出现有候选。

## 兼容、安全与审计

- SSH Target 绑定、服务映射人工确认、工具白名单、Evidence Gate、模型预算、调用次数、Docker 权限和零自动重试保持原状。
- 不增加 DB 表，不修改历史 Run、证据、人工审核或映射；协议与结果保存在 Run 现有配置/摘要快照中。
- 失败模型响应和完整 Prompt 不持久化。审计仅记录安全字段路径、错误类型、被隔离的假设类别与 Runtime 是否覆盖结束建议。
- 前端显示协议、Runtime 结论层级、直接故障服务与状态，以及局部隔离/结束建议覆盖的安全审计信息。
- 本地部署和 Fake Provider 测试不等同于真实模型或生产环境验收；部署后须由操作者在页面手动发起下一次 Run。

## 改动与验证

核心实现位于 `backend/app/agent/decision_runtime.py`；协议接线及旧流程兼容在 `backend/app/agent/workflow.py`、`backend/app/agent/business_investigation.py`；配置快照和模型调用审计在 `backend/app/settings.py`、`backend/app/api/v1.py`、`backend/app/context/model_errors.py`；Run 结果展示在 `frontend/src/main.tsx`。回归覆盖见 `tests/test_v23_decision_runtime.py`，旧 V2.2 测试通过 Fixture 保持旧协议。

验证结果：定向后端回归 **170 passed**；相关 Python 文件 Ruff 通过；`backend/app` Python 语法编译通过；前端 TypeScript 及生产构建通过。没有调用真实模型、创建生产 Run、连接生产服务器或执行 SSH/Docker 生产工具。

## 本地部署验收

本地部署已完成：使用 Compose `up -d --build --no-deps api web` 仅重建了本地 API/Web，没有执行 `down`，没有删除或重建 Volume。最终审阅修正后第二次重建前也确认活动 Run、排队/运行任务及独立模型 Worker 均为 0。

- 实际 Settings：协议 `minimal_v2`；模型 `gpt-6.1-sol`；推理 `low`；请求超时 180 秒；单次输入预算 30,000；累计预算 1,000,000；模型输出上限 1,500；模型轮数上限 100；工具调用上限 500。上述原有模型/预算值保持不变，本轮只在 `.env` 增加 `AGENT_DECISION_PROTOCOL=minimal_v2`。
- 新 Run 协议选择静态核验为 `minimal_v2`；无协议快照的历史 Run 仍回退 `legacy_v22`。
- API 容器为 `healthy`；`/api/v1/health` 与 Web 首页均返回 HTTP 200。
- 数据仍位于原 `atr-runtime_runtime-data` Volume。部署前后 SQLite 计数一致：业务画像 1、SSH Target 4、服务绑定 30、历史 Run 24、证据 106、工具审计 113、模型用量记录 73、图快照 36、任务 41；部署后无活动 Run、任务或模型 Worker。
- 未执行真实模型调用、生产 Run、生产 SSH/Docker 工具或服务器操作。

API 重建清除了仅驻留在进程内存的 SSH 会话凭据；下一次手动调查前，操作者需要在 ATR Web 重新提交 SSH 会话密码。持久化 Target、授权映射与数据库内容均保留。

下一次人工验收步骤：在 Web 选择已授权业务、明确选择目标 SSH Target 和调查模式；重新提交该 Target 的短期 SSH 会话密码；启动新的 Run。先确认 Run 摘要中的协议为 `minimal_v2`，然后检查工具审计的实际 Target ID、Runtime 结论层级、Evidence Gate 状态和模型用量记录。没有故障证据时应接受“未观察到故障”或“证据不足”，不要将其改写为根因结论。
