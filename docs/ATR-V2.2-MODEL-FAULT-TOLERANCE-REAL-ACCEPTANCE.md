# ATR V2.2 模型决策局部容错与真实验收

## 结论

模型决策局部容错修复、回归和本地部署已完成。最近一次已完成的真实只读 Run 因当时 Target 的本地累计调用上限 `max_calls=100` 停止，不是模型校验或远端网关拒绝。之后按用户最新要求将本地 Run 上限设为 100/500、累计 Token 设为 1000000，并把该 Target 的生命周期调用上限调到 10000；API 已重建并读取新值。等待操作者重新提交短期 SSH 会话后，再启动一轮新的真实只读 Run。没有证据确认随想记当前存在真实故障。

## 修复内容

历史失败 Run `ee41eebd-04ff-456b-a250-bd98ffc4d13b` 的既有安全诊断为 `hypotheses[1].evidence_links[1].relation / counterevidence_target_mismatch`。原始模型响应未持久化，因此不能确认当时关系的具体内容。

此前 `validate_report` 会把一条跨目标 `refutes` 与未知节点、未知证据、非法预测或非法动作一样处理，拒绝整份模型决策。现在在严格校验结构、节点、证据 ID、预测和动作之后，仅隔离目标与证据不匹配的局部 `refutes`：移除这条关系，不将其改写为 `supports`，保留其他合法关系，并记录安全诊断。受影响假设不能通过 Evidence Gate，置信度最多为 0.25。

首次出现此类局部错误时，工作流可以继续执行本轮候选集内的授权只读动作；连续两轮出现时会停止后续动作。没有自动重试、额外模型轮数或权限放宽。未知证据、非法节点、错误角色、越权工具和无效 action_id 仍会拒绝整份决策。

## 修改文件

- `backend/app/agent/model_schema.py`：隔离局部无效反证关系并生成安全诊断；其余引用校验保持严格。
- `backend/app/agent/workflow.py`：将局部诊断传给 Evidence Gate；受影响假设不能被支持。
- `backend/app/agent/business_investigation.py`：持久化安全审计，允许首次错误后继续，并对连续错误有界停止。
- `frontend/src/main.tsx`：显示被隔离数量、字段路径和停止状态。
- `tests/test_final_reference_contract.py`：覆盖关系隔离、合法关系保留、Gate/置信度保护、继续一次授权只读工具、重复错误停止及非法动作整体拒绝。
- `docs/ATR-V2.2-MODEL-FAULT-TOLERANCE-REAL-ACCEPTANCE.md`：本报告。

## 本地验证与部署

- Ruff、Python 语法检查通过。
- 定向回归：`test_final_reference_contract.py` 与 `test_baseline.py` 共 57 项通过；历史反证保留专项 1 项通过。按本轮约束未运行完整测试套件、长 Fake Provider 流程或额外真实模型探测。
- 前端构建通过；API 和 Web 镜像构建成功。
- 仅重建本地 `atr-runtime` API 与 Web，没有执行 `down`、删除数据卷或重置数据库。
- 部署后 API 健康检查和 Web 均返回 HTTP 200；API 状态为 ready。`atr-runtime_runtime-data` 仍存在，创建时间保持为 `2026-10-10T01:44:39Z`。
- `suixiangji` 的 4 个 SSH Target 均仍存在；所选目标的 API/DB 人工确认映射及授权配置保留。当前所选目标会话凭据仍为 ready。
- 模型、超时、预算、轮数与工具上限均未修改；运行配置为 `gpt-6.1-sol`、low、超时 180 秒、单次输入 30000、累计 200000、输出 1500、模型最多 8 次、工具最多 20 次。

## 首次真实只读验收

- 业务：`suixiangji`。
- SSH Target：`d997a5d5-8898-4f99-bbe7-765e38facac9`（`suixiangji安全网关2`，`forced_gateway`），Run 绑定状态为 `bound`。
- Run：`7d36335f-c74a-4c73-bb58-3ff045c88010`；后台任务已完成。
- 时间：2026-10-12 11:18:52 至 11:26:25（北京时间）。
- GPT：8 次请求全部完成，无 SDK 重试；模型状态为 `completed`，没有模型请求或全局 Schema 错误。
- 工具：9 次，全部成功；仅执行 `container.status`、`container.logs`、`config.list` 和 `config.read` 等只读工具。证据共 9 条。
- 模型决策：最终保留 7 个假设，其中 6 个被反驳、1 个仍为候选；没有假设通过 Evidence Gate。
- 局部错误：第 8 轮发现 1 条 `hypotheses[0].evidence_links[1].relation` 校验失败，错误类型 `counterevidence_target_mismatch`，阶段 `validate`。审计未保存原始响应或关系内容。该错误被局部隔离，Run 未转为 `model_error`，也未创建越权动作。由于它发生在最后一轮，之后没有更多模型轮次或工具动作；Run 按原配置以 `round_limit` 正常结束，终态 `unresolved`。
- 是否确认生产故障：否。此次只读证据不足以独立确认根因；不能把未证实假设写成真实故障结论。
- 任务状态：只有本次一个新 Run；完成后没有剩余运行中调查。

## 验收判断

**A — 工程链路：通过。** 修复后的 API/Web 已部署，预算与授权边界未变。

**B — 真实多轮调查：通过至运行上限。** GPT 完成 8 轮，9 次只读工具均成功，Run 以正常轮数上限结束。

**C — 独立确认生产根因：未通过，且本次没有证据支持伪造结论。** 最终状态为 `unresolved`，尚无已证实故障。

本次验证证明局部无效证据关系不会再必然导致整份决策失败；并未证明所有可能的模型结构错误都可恢复，也没有测试未覆盖的生产故障。

## 安全边界与后续

本次及后续获准 Run 通过已授权的 `forced_gateway` 对生产目标执行了只读 SSH 查询；没有执行服务器写操作或故障注入，没有修改映射、人工审核、历史证据或生产配置；没有 Push、Merge 或创建 PR。未输出密码、密钥、原始 Prompt、原始模型响应或敏感日志。

上一次验收结束时，Run 已达到当时配置的 8 次模型调用上限。本报告不建议为得到“成功”状态而重跑；后续是否继续验收及调整上限由用户明确决定，调整情况见下文。

## 后续调用上限调整（2026-10-12）

- 仅将本地 `.env` 的 `INVESTIGATION_MODEL_ROUNDS` 从 8 调整为 20、`INVESTIGATION_TOOL_CALLS` 从 20 调整为 50。代码允许的上限分别为 20 和 100。
- Compose 解析值及重建后的 API Settings 均确认是 20/50。单次输入 30000、累计预算 200000、输出上限 1500、超时 180 秒、推理级别 low、模型和 Provider 均保持不变；初始阶段调用上限仍为 6。
- 仅重建本地 API；API/Web 返回 HTTP 200。数据卷、历史 Run、4 个 SSH Target、API/DB 人工确认映射保留。旧 Run 仍保持自己的历史预算快照；后续新 Run 会读取 20/50。
- API 重建曾清除进程内短期 SSH 会话凭据；操作者随后通过 Web 重新提交，本次 Run 启动前状态为 ready/authorized。当前 Run 已终止；下一轮如 API 再次重建或会话过期，需重新提交密码。

## 20/50 配置后的新 Run（2026-10-12）

- Run：`8384031e-c29c-4f73-a02c-7c71bf2cbf51`，目标仍为 `d997a5d5-8898-4f99-bbe7-765e38facac9`，绑定状态 `bound`；Run 预算快照为 `max_rounds=20`、`max_tools=50`。
- Run 已进入终态 `unresolved`。实际完成 15 次模型请求、17 次只读工具调用，17 次工具均成功并产生 17 条证据。决策校验没有出现局部关系错误或 ModelDecisionError。
- 第 16 轮准备模型请求时触发 `BudgetExceeded`，`stop_reason=total_token_limit`；预检拒绝该请求，未发给模型供应商。累计记录输入 181767、输出 7130，合计 188897 Tokens；累计预算仍为 200000。调查因此没有到达 20 次模型调用上限。
- 本次没有证实随想记故障根因。当前 20 次调用上限是最大值，不能覆盖累计预算。若要在后续 Run 中为 20 次调用留出空间，需要单独提高累计预算；本次没有改动 `BUDGET_TOTAL_TOKENS=200000`。

## 300000 累计预算部署与后续 Run（2026-10-12）

- 按用户确认，将 `.env` 的 `BUDGET_TOTAL_TOKENS` 从 200000 调整为 300000；其余验收设置保持为单次输入 30000、模型最多 20 次、工具最多 50 次、输出 1500、超时 180 秒、推理级别 low、模型 `gpt-6.1-sol`。
- 重建前确认没有排队或运行中的 Run。仅重建 `atr-runtime` API；重建后 Compose 和 API Settings 均读到累计预算 300000。API 健康检查返回 200、容器为 healthy，Web 返回 200。SQLite 卷未重建，历史 Run 数仍为 18（启动本次 Run 前）；4 个 Target 和 30 条服务绑定记录仍在。
- 操作者通过 Web 重新提交短期 SSH 会话凭据后，Target 状态为 `ready/authorized`。新建事件：`ca3395f8-a73e-442a-9236-c2e8adc22285`；新 Run：`5adba235-e63e-4736-86e1-211499504f2c`，绑定 `d997a5d5-8898-4f99-bbe7-765e38facac9`（`suixiangji安全网关2` / `forced_gateway`）。预算快照确认单次输入 30000、累计 300000、20 次模型、50 次工具。Run 启动时没有其他活跃任务。
- Run 以 `unresolved` 结束：2 次 GPT 请求均成功，实际输入合计 20007、输出 607，SDK 重试 0；没有 Schema 或引用校验错误。4 次工具尝试中，`container.status`、`container.logs`、`config.list` 成功并产生 3 条证据；`config.read` 被拒绝，未执行后续调查。累计预算仅使用 20614 Tokens，故 300000 总预算和 20/50 Run 上限均不是本次停止原因。
- 可核验的失败审计为 `tool_id=config.read`、请求路径 `/opt/suixiangji/atr-reviewed-config/deployment.reviewed.json`、异常 `PermissionError`、本地诊断 `target_call_limit`。当时 Target 配置 `max_calls=100`；本 Run 前已有 97 条 `runtime_observations`，前三次调用达到 100 次上限，第四次尝试被本地 `RuntimeService.execute` 拒绝。该错误不是 Linux 文件权限、`atr-read-v1` 服务端策略或 SSH 主机验证拒绝。API 对 Run 的通用 `stop_reason` 显示 `permission_blocked`，描述比底层原因更宽泛；底层观测已记录具体额度错误。
- 本次没有确认生产故障根因，未完成完整的多轮证据闭环。没有通过删除审计或清零计数来绕过额度，也没有自动提高 Target 额度或再启动 Run。要完整再验收，必须先单独批准提升这个 Target 的生命周期 `max_calls`；按其当前累计 101 次观测和单 Run 最多 50 次工具计算，建议设为 160（保留约 9 次余量），然后再创建一个新 Run。此设置独立于 20/50 Run 限额，并会增加该 Target 未来可执行的只读调用次数。

## 扩大验收额度与本地部署（2026-10-12）

- 按用户最新要求，`.env` 仅将 `INVESTIGATION_MODEL_ROUNDS` 调为 100、`INVESTIGATION_TOOL_CALLS` 调为 500、`BUDGET_TOTAL_TOKENS` 调为 1000000；`INVESTIGATION_INPUT_TOKENS=30000`、`MODEL_MAX_OUTPUT_TOKENS=1500`、`MODEL_TIMEOUT_SECONDS=180`、`MODEL_REASONING_EFFORT=low` 保持不变。
- 后端 Settings 和 `BudgetLimits` 的校验上限同步为 100 次模型、500 次工具；累计 Token 的既有上限已支持 1000000。新增预算快照回归测试，先按旧约束复现失败，修改后通过。
- 预算相关定向测试 29 项通过，Ruff 对改动文件通过。全量测试结果为 566 通过、18 失败：14 项因本机缺少 Playwright Chromium 可执行文件；另 4 项在现有决策容错测试中出现断言不匹配（两项有界纠错行为、一项调用预算行为、一项局部反证关系隔离）。这些失败未由本次预算字段修改触及，仍记录为未通过项。
- 按当前 Task 变更，仅更新目标 `d997a5d5-8898-4f99-bbe7-765e38facac9` 的本地 `runtime_targets.config_json.max_calls` 从 100 调为 10000；事务后逐字段比较确认其他目标配置不变，Target 的 101 条运行观测审计未增删。数据库中的历史 Run、证据、绑定及其它 Targets 未修改。
- API 使用新代码重新构建并重建；Settings 读取值为累计 1000000、单次输入 30000、模型 100、工具 500、输出 1500、超时 180、low。API 和 Web 的 HTTP 检查均返回 200，SQLite 卷仍为 `atr-runtime_runtime-data`，创建时间不变。API 重启清空短期 SSH 内存凭据；新一轮启动前需要操作员重新从页面提交。
- 部署完成时新配置尚未用于新的 Run；随后启动的 100/500/1000000 Run 结果单独记录于下一节，不将上一节的 `5adba235-e63e-4736-86e1-211499504f2c` 的 20/50/300000 结果当作新配置验收结果。

## 100/500/1000000 配置后的真实只读 Run（2026-10-12）

- 操作者在 Web 重新提交短期 SSH 会话凭据后，新建事件 `a2d52b3f-8dbe-42f4-b2da-e0bf07535a9c` 并启动 Run `2149ddaa-7af7-49de-822d-3155f906d58c`。Run 明确绑定 `d997a5d5-8898-4f99-bbe7-765e38facac9`（`suixiangji安全网关2`，`forced_gateway`），终态为 `unresolved`。
- Run 预算快照确认为单次输入 30000、累计 1000000、最多 100 次模型调用及 500 次工具调用；未触及预算限制。
- 5 次只读工具调用全部成功并绑定同一 Target，覆盖容器状态、日志及授权配置清单/读取；共形成 5 条证据。GPT 前 3 次请求成功，第 4 次请求在供应商请求阶段返回 HTTP 529，安全诊断为 `provider_unavailable` / `APIError`，网络阶段约 106734 ms、总计约 109897 ms，重试 0。该请求没有有效响应，后续调查按设计停止；因此本轮没有完成根因闭环，也不能据此确认生产存在故障。
- 本 Run 共记录 4 次模型请求（3 成功、1 失败）、5 次成功工具调用和 5 条证据；报告不保存原始 Prompt、响应或敏感日志。此次失败是供应商暂时不可用响应，不是 Target 调用额度、ATR 本地请求输入预算、模型输出校验或远端工具权限拒绝。
- 终止后确认没有运行中 Run；API/Web 均 HTTP 200，API 容器 healthy。`atr-runtime_runtime-data` 卷仍存在且创建时间未变；4 个 Target、人工确认映射及历史数据保留。没有生产写操作、故障注入或 Push/Merge/PR。
- 该 Run 证明新预算与调用上限已被新 Run 正确读取，且目标 SSH/只读工具链路可工作；它不满足“一次完整自主调查闭环”验收。当时该轮结束后没有自动创建第二个 Run；操作者随后明确要求完整通过并授权继续复验，结果记录于后续章节。
- 全量测试此前为 566 项通过、18 项失败（14 项因缺少 Playwright Chromium，4 项决策容错相关断言未通过）；预算定向测试 29 项通过。上述失败状态仍未解决，不作为全量回归通过报告。

## 用户要求完整通过后的两次复验（2026-10-12）

- 两次均使用当前 100/500/1000000 预算，显式绑定同一 `forced_gateway` Target；启动前无活跃 Run。两次均只执行只读工具，没有目标错绑或工具调用失败。
- Run `6677563d-529a-4e0d-9b46-37a808a73272`：5 次 GPT 成功，第 6 次 GPT 请求收到供应商 HTTP 500 / `InternalServerError` / `provider_unavailable`，请求阶段耗时约 105582 ms、重试 0。7/7 工具成功，7 条证据、6 个假设；Run 安全停止为 `unresolved`。
- Run `727e6691-25fb-4655-8fce-e3eab3b1fb4e`：第 1 次 GPT 成功，第 2 次 GPT 请求收到供应商 HTTP 500 / `InternalServerError` / `provider_unavailable`，请求阶段耗时约 101188 ms、重试 0。3/3 工具成功，3 条证据；Run 安全停止为 `unresolved`。
- 这两次失败均发生在供应商请求阶段，且没有有效响应；预算未触顶。结合前一 Run 的 HTTP 529，可确认本轮多个独立 Run 遇到中转服务 500/529 暂不可用响应。没有证据表明 ATR 预算、SSH 授权、工具白名单或证据校验导致停止。
- 最后一轮后确认活跃 Run 为 0，API/Web 均 HTTP 200。完整多轮闭环验收仍未通过；没有将 `unresolved` 描述为通过或真实故障结论。为避免无限重跑，在这两次复验后停止；后续需待模型中转服务恢复稳定后再启动验收。
