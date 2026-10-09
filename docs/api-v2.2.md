# V2.2 API 增量

沿用 [V2.1 API](api-v2.1.md)，所有管理/诊断接口仍需 Operator Bearer token。默认关闭外部模型/SSH，固定只读工具和人工绑定不变。

| 入口 | 增量行为 |
|---|---|
| POST /incidents | mode 增加 graph_adaptive；normal 在业务画像入口走自适应；旧 Collector 的显式 graph_adaptive 返回 409 |
| POST /incidents/{id}/runs | 同事件活动任务原子复用，返回同 job_id，不重复提交或建立预算 |
| GET /runs/{id} | 摘要含 stop_reason、每轮假设、反证历史、scope_requests、counterexamples；预算/usage 保留估算种类 |
| GET /businesses/{id}/bindings | 绑定区分人工审核与 freshness_status/observed_at/expires_at/source/current；失败发现保留授权 |
| 固定清单工具 | name_hints 最多 8 条；limit/cursor 受 Schema 限制；输出 truncated/partial/timeout、扫描量和 next_cursor |
| GET /knowledge-counterexamples | 最近 100 次已结束运行的未验证反例；不会自动成为经验或 Trusted |
| GET /patterns | health 反馈计数/needs_review 与 compatibility；历史 Trusted 审核状态保留 |
| POST /patterns/{id}/review | Candidate 提升前检查实际验证、修复记录和当前版本兼容 |
| POST /pattern-matches/{id}/outcome | 原验证/有用性反馈，事务内复查，负反馈产生复核事件并阻止后续建议 |
| POST /evaluation-trials | arm 增加 graph_adaptive；指标从持久化运行派生；同组约束保留 |
| GET /metrics | 增加 three_arm_evaluation，只统计三臂完整的同组；部分诊断与误报分开 |

以上路径以 /api/v1 为前缀。旧 /evaluation-pairs 仍原子记录两臂。第三臂使用 /evaluation-trials 添加到同 pair_key/repetition，全部完成才进入三模式指标；现有 UI 两臂写入表单保留，三臂批量写入可用 scripts/evaluate_faults.py 或 API。

标准评测 ground_truth.truth 是服务名到机制的映射，例如 `{ "truth": { "store": "database_unavailable" } }`。评分要求支持集合与真值完全一致；支持了集合外原因记误报，只找到多故障的一部分记部分诊断，unresolved 不能填为 correct。非标准真值仍由操作者评分，API 不把人工评分证明为独立事实。真值不会提供给模型。

停止原因：evidence_supported、model_budget_exhausted、tool_budget_exhausted、context_budget_exhausted、round_limit、no_new_actions、permission_blocked、evidence_conflict、action_rejected、provider_error。未实现用户取消接口，也没有自动修复接口。
