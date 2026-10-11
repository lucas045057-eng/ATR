# ATR V2.2 模型结构化输出兼容性修复报告

日期：2026-10-10。修改范围：`E:\ATR`。本轮未调用真实付费模型 API，未提交、推送、合并或创建 PR。

## 1. 本次历史调查的可核验证据

已先读取现有数据库的 usage、run summary 和事件审计，安全副本位于 `test-results/output-compatibility/historical-safe-audit.json`。

| 项目 | 已保存的事实 |
| --- | --- |
| run_id | `2d02160a-b6c6-4cfd-bd4f-3a4703484428` |
| request_attempt_id | `76630f78-38e5-480e-857a-5c607b15107b` |
| 模型 / provider | `gpt-6.1-sol` / `openai-compatible` |
| usage | actual：输入 2219，输出 844；实付费用未知 |
| 错误 | `model_schema_error` / `ValidationError` / `validate` |
| HTTP 状态 | 审计记录为 None，不人为补写 |
| 栈位置 | `Report.model_validate`，主报告结构校验 |
| 原始响应 / 字段级错误 | 未保存，无法还原具体失败字段 |

**不能断言这次历史输出具体哪个字段失败，也不能断言下述合成变体就是本次历史失败的原因。** 未重放历史模型请求，未改写历史错误记录。

## 2. 审计发现与可复现修复

- 旧 Prompt 未明确可选数组的空值约定；`verification_predictions: null` 和 `scope_requests: null` 被旧 Schema 拒绝。现明确接受省略、`[]`、`null`，统一为 `[]`。
- `next_action` 是说明文本，旧 Schema 拒绝 `null`。现仍要求字段存在，允许字符串或 `null`，后者统一为空字符串；不生成任何执行动作。
- 单个完整 JSON 代码块在旧解析器中失败。现接受裸 JSON、普通空白或完整的单个 `json` / 无语言标记代码块。仍拒绝旁白、多个代码块、非对象输出和重复 JSON 键，不从任意文本中抓取片段。
- 空内容原先被替换为 `{}`，导致误报缺失 Schema 字段。现保持为空，并准确归类为 `model_invalid_json` / `parse`。
- 输入证据使用 `tool`、输出预测使用 `tool_id`。现输入输出统一使用 `tool_id`。
- 旧 Prompt 的 “Return unresolved” 未指明字段。现明确是可选假设 `status`，禁止顶层 `status`；模型的自报状态仍不构成证据。

以上兼容性问题均用本地 Fake Provider 或隔离工作流复现，并保留自动回归。未做字段别名猜测、数字字符串转换或必填字段补造。

## 3. Prompt 与 Schema 的统一约定

Prompt 的输出契约由 Pydantic Schema 生成，使用当前模式的动作 Schema；字段名称、必填列表、枚举和长度限制来自同一份定义。

| 字段 | 最终约定 |
| --- | --- |
| `summary` / `hypotheses` | 必填；字符串 / 最多 3 个假设；`hypotheses: []` 合法 |
| `next_action` / `action` | 均必填；说明文本可为 null；动作可为 null |
| 假设必填字段 | `target_node_id`、`target_role`、`failure_mode`、`causal_chain`、`prediction`、`rationale`、`evidence_links`、`missing_evidence`、`confidence` |
| `causal_chain` / `missing_evidence` | 2–12 个字符串 / 最多 12 个字符串；`missing_evidence` 不允许省略或 null |
| `confidence` | 0..1 的数值；整数 0/1 合法；布尔值、数字字符串不合法 |
| `evidence_links` | 最多 24 条；每条必填 `evidence_id`、`relation`、`explanation`；relation 仅 supports/refutes |
| 假设 `status` | 可省略或 null；枚举仅 candidate/supported/refuted/unresolved |
| `verification_predictions` / `scope_requests` | 可省略、空数组或 null；最多 4 条预测 / 12 条范围申请 |
| 自适应动作 | 仅 `{action_id, rationale}`，且 action_id 必须属于本轮候选 |
| 旧模式动作 | 仅 `{tool_id, target_node_id, rationale}`，工具与目标组合必须属于本轮候选 |

保留 `strict=True` 和 `extra="forbid"`。拒绝额外参数、命令字符串、未知节点、角色不匹配和未知证据引用。

预测仍是待验证的决策。校验和证据门共享组合规则：状态预测仅状态工具、eq/ne、合法状态字符串；HTTP 预测仅 health.get、eq/ne、100..599 整数；监听预测仅 network.listeners、eq/ne、布尔值；日志预测仅日志工具、contains、6..160 字符具体信号。错误组合按准确的 tool_id、operator 或 value 字段指出原因。

## 4. 安全的字段级诊断

结构错误标记 `validation_kind=format`；引用、动作和预测语义错误标记 `validation_kind=decision`。对外保留 `model_schema_error`，两类失败均禁止后续工具执行。

诊断条目仅包含字段路径、固定错误类型和阶段，例如合成测试输出：

```json
{"path":"hypotheses[0].confidence","type":"float_type","stage":"validate"}
```

- 不记录 Pydantic 的 input、msg、ctx、URL 或原始异常全文，不记录原始 Prompt、模型响应、密钥或错误字段的值。
- 字段路径仅允许已知 Schema 名称和受限数组下标；模型自造的字段名显示 `<extra_field>`。每次最多记录 24 条，每条最多 16 层路径。
- Pydantic 的 str/int/bool 联合类型分支被安全地归并到实际 `.value` 字段，避免被误报为额外字段。
- 安全诊断同时进入 usage 审计、失败事件、受控日志和 run summary；API 与页面显示中文原因、路径、类型和阶段。
- 历史记录缺少字段级诊断时，页面明确显示“此记录未保存字段级诊断，无法确认具体失败字段”，不猜测或补写。

无需新增数据库迁移，继续使用已有 `diagnostics_json`。历史 usage 和本次历史 run 保持原样。

## 5. 输入预算与安全边界

生成 Schema 增加了 Prompt 长度。只读审查复现了代表性后续调查从原估算 11,531 增至 14,125、超过现有 12,000 上限的问题，现已修复：

- 使用和 `reserve()` 相同的脱敏 JSON 序列化及 UTF-8/framing 估算，读取持久化预算上限。
- 仅移除请求预览中已完整出现在 previous_hypotheses 的重复历史项；原状态和数据库历史不变。
- 超限时逐级缩短复制的证据文本预览，保留首尾并标记 truncated；保留全部已选证据 ID、状态值、候选动作、服务 ID 和反证引用。
- 不增加预算或配置；不可进一步压缩的请求仍在供应商调用前安全停止。

复查估算：ASCII 用例 11,616、Unicode 用例 11,128，均低于 12,000；原状态与调用前深拷贝一致。

独立模型 worker、硬截止时间、零自动重试和既有供应商错误治理均保留。验证在成功结算和 Agent 后续处理之前完成。未修改 store 授权限制、工具白名单、运行绑定校验、假设反证保留或独立证据门。格式合法且高置信的候选仍可能得到 unresolved；模型自报 supported 不能绕过证据门。

## 6. 验证记录

最终完整回归：**284 passed，0 failed，0 errors，0 skipped，441.29 秒**。JUnit 已单独解析核对计数。Ruff、依赖锁检查、前端 TypeScript/构建、Docker api/web 构建均通过。

新增 38 项输出兼容性与安全诊断检查，另新增 1 项真实浏览器字段提示检查。覆盖必填、枚举、null、数字类型、JSON 包装、重复键、未知引用、非法动作、预测组合、Unicode 输入预算、不可压缩请求阻止和原状态不变。先观察兼容性、页面、预算和字段路径用例失败，再实现修复；旧安全测试未放宽。

只读审查发现的输入预算问题已修复并复查通过，无剩余 Critical / Important 问题。

Docker Mock 使用独立项目 `atr-output-compatibility-test` 和独立测试数据目录；API 仅接入 internal 网络，供应商指向该网络内 Fake Provider。本轮最终 Docker 验收只产生 9 次本地 Mock 模型请求，无自动重试、无 store 读取；开发过程的其他测试同样只用本地 Fake Provider。

| Docker 场景 | 预期结果 |
| --- | --- |
| 正常输出 / 合法 null / 单个 JSON 代码块 | 严格校验与独立证据门通过，supported |
| 错误 confidence 类型 | format；显示 hypotheses[0].confidence 和 float_type |
| 未知证据 | decision；unknown_evidence；停止后续工具 |
| 非候选动作 / 额外动作参数 | decision；action_not_allowed / extra_forbidden；停止后续工具 |
| 不匹配角色 | decision；target_role_mismatch；停止后续工具 |
| HTTP 529 | provider_unavailable；未自动重试；保留现有证据 |

所有场景保留两条初始证据和两次已授权初始读取，预留预算释放；真实 wire usage 存在时按 actual 记录，529 未提供 usage 时按 estimate 记录，费用未知。真实 Chrome 检查了格式、证据、动作和额外参数的字段提示，未显示模拟敏感值或密钥。

证据：`test-results/output-compatibility/final-regression.xml`、`docker-acceptance.json`、`review-fixes.xml`、`invalid_confidence.png`、`unknown_evidence.png`、`invalid_action.png`、`extra_action.png`。

最终新镜像已更新到本地运行容器。更新前检查无活动任务；更新后 API 为 healthy，API 28000 / Web 28080 的健康接口均返回 200。42 张表完整行摘要及 8 个上传文件 SHA256 均一致，4 条历史调查可读取，Catalog-Test 仍为 approved v12；`.env`、compose、模型、Base URL、授权和超时配置均一致。

真实 Chrome 只读打开了指定历史 run 的页面，验证内部尝试编号未变、仍显示 ValidationError，并明确告知“此记录未保存字段级诊断，无法确认具体失败字段”。未按下模型调用按钮。部署证据：`before-deployment-checks.json`、`after-deployment-checks.json`、`historical-safe-panel.png`。隔离 Mock 服务已停止，测试数据保留。

## 7. 数据保留与回退

配置及 42 张表（包含历史 diagnostics_json）的完整行摘要已保存为校验基线；上传文件也保存 SHA256。现有 SQLite 的一致性备份位于 `/data/runtime.sqlite3.before-output-compatibility-20261010.bak`。

已保存本轮开始时正在运行的镜像：

- `atr-runtime-api:before-output-compatibility-20261010`
- `atr-runtime-web:before-output-compatibility-20261010`

本轮没有变更数据库结构，必要时可在无活动任务时把这两个本地镜像恢复为当前镜像并重启 api/web；无需回退 Schema 或覆盖数据库。未执行回退。任何回退都应继续挂载原 Volume，禁止删除 Volume 或覆盖最新业务数据。

## 8. 适用范围与限制

本轮验证覆盖确定的合成兼容性问题和非法决策拒绝行为。未验证真实供应商会产生哪些变体，未追溯出历史 run 的具体字段错误。新诊断适用于后续调用，不能还原未保存的历史原文。

模型调用配置、Base URL、授权配置及 120 秒超时保持不变。预览压缩可能减少模型看到的日志细节，truncated 明确标记这一点；证据门始终使用完整已保存证据。

```text
IMPLEMENTATION_STATUS=completed
TEST_RESULT=284 passed; 0 failed; 0 errors; 0 skipped
DOCKER_HEALTH=API healthy; 28000/28080 health=200
STRUCTURED_OUTPUT_COMPATIBILITY=合法变体通过；非法动作与引用拒绝
MODEL_ERROR_UI=安全字段路径、类型、阶段可见；历史缺失诊断明确提示
DATABASE_COMPATIBILITY=Schema不变；42张表和8个上传文件完整保留
HISTORICAL_FAILURE_FIELD=unknown（无可核验原始响应或字段诊断）
REAL_PAID_REQUESTS=0
GIT_PUSHED=false
GIT_MERGED=false
PR_CREATED=false
```
