# Context Budget Manager

## 默认与配置

预算在 Python 工具/模型层执行，不是仅写入 Prompt。SQLite 中每个初始理解/调查预算保存限额、已用量、原子预留和停止原因。

| 参数 | 默认 | .env 配置 |
|---|---|---|
| 初始理解每请求输入 | 8,000 | INITIAL_INPUT_TOKENS |
| 调查每请求输入 | 12,000 | INVESTIGATION_INPUT_TOKENS |
| 初始理解最大模型调用 | 6 | INITIAL_MODEL_CALLS |
| 调查最大模型轮数/调用 | 8 | INVESTIGATION_MODEL_ROUNDS |
| 调查最大工具次数 | 20 | INVESTIGATION_TOOL_CALLS |
| 单预算总输入+输出 | 100,000 | BUDGET_TOTAL_TOKENS |
| 单请求最大输出 | 1,500 | MODEL_MAX_OUTPUT_TOKENS |
| 模型声明窗口 | 32,768 | MODEL_CONTEXT_WINDOW |
| 源码检索条目 | 20 | CODE_SEARCH_LIMIT（请求值也受其约束） |
| 单预算检索字节 | 500,000 | 通过预算 limits 修改 |

业务创建产生初始预算，模型理解复用最新初始预算及历史用量。每个调查产生独立预算。页面可修改业务已有预算；修改不会归零 calls/tokens/tools/bytes。初始理解停止后，提高预算再点击分析可继续；已经结束的调查不能从任意步骤自动断点续跑，需要新调查。

API：GET `/api/v1/businesses/{id}/budgets`，PUT `/api/v1/businesses/{id}/budgets/{budget_id}`。PUT 为 BudgetLimits 完整 JSON，例如：

```json
{
  "max_input_tokens": 8000,
  "max_calls": 6,
  "max_total_tokens": 100000,
  "max_tools": 20,
  "max_retrieved_bytes": 500000
}
```

## 调用前与结算

请求前估算采用 UTF-8 字节数加每条消息 framing 余量与固定余量，是偏保守的 token 预算代理，不是精确 tokenizer 计费。加上最大输出预留，原子检查单输入、声明窗口、调用次数、总 token，失败则不进入 LiteLLM。工具/检索字节也在下一次动作前校验，超限保留已完成解析、证据和原因。

供应商返回 prompt_tokens/completion_tokens 时记录 actual；缺少 usage 时记录 estimate，失败可能已收费因此保留保守估算。模型名、每次输入/输出、累计量、检索字节、calls、tools、stop_reason 均持久化。若实际 usage 高于预留，标记 provider_usage_exceeded_reservation 并阻止后续请求，直到操作者调整预算。不同供应商 token/额外计费方式仍须真实适配验证，不能用当前估算承诺精确账单。

费用默认 null/unknown。仅当 MODEL_INPUT_COST_PER_MILLION 与 MODEL_OUTPUT_COST_PER_MILLION 均配置时按其计算，cost_kind=estimate_configured_rates，即使 token actual 也不是供应商账单金额。配置单位是每百万 token 的费用，币种由操作者确定；本版不抓取价格、不硬编码即时费率。

无自动模型重试；无效 JSON/超时也记录结算。后端重启使 pending 记录 interrupted 并估算结算，不能以重启释放已可能消费的预算。

## 上下文选择

初始理解最多选服务 25、模块 25、接口 20、已完成索引 40，优先配置/源码元数据。按预算逐项移除索引及较低优先摘要，保持完整 JSON，不截断序列化正文。始终不提供初次源码全文。

调查使用当前已审核画像、选定服务链路、至多 40 节点/边摘要、有限当前观测、短日志尾部、少量相关 Pattern 与局部代码。代码通过业务限定的 search/context 工具读取，记录 file_id/digest/version/行号和预算字节。全部历史日志、原配置、旧故障记录不默认输入。

大量无关文件只扩大有界本地清单，不无限扩大模型上下文。静态 AST/哈希/树/配置解析不消耗模型 calls。超低预算仍可能无法容纳最小请求，明确停止，不以脱离预算的旁路完成。

## 能力与离线

GET `/api/v1/model/capabilities` 返回配置声明、JSON、text、vision_declared、vision_adapter=pending、context_window、usage 策略与模型名。没有声称在线能力探测。未启用外部分析、无模型/key、不支持 JSON、返回无效结构均保留静态画像并标 pending；图片能力声明不会伪造图片结果。

测试验证低预算零调用、增额恢复、原子预留、实际 usage 与估算区别、工具/字节限制、初始请求无源码正文。模型响应由测试 stub 提供，未在本机验证真实供应商计费或 usage 格式。
