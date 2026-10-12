# ATR 模型决策契约统一实施计划

**目标：** 统一既有发现报告与调查报告的 Schema、Prompt 和严格校验，不改变报告格式、业务授权、模型配置或历史数据。

**依据：** 本轮用户四阶段任务；实际 Run bb882ea5-cd94-413b-bdd9-4553cfab99c7 的安全错误路径 proposals[0].next_tools 与 discovery.py:57。

**实现：** 沿用 MappingReport/Report 与现有 Pydantic 类型。动态引用、节点角色、候选对象类型/证据/建议工具、动作组合及预测组合由相同函数生成；Prompt 使用当前有效集合。只读预算缩减候选时重新生成 Schema，不自动扩大集合。

## 执行与验证

- [x] 只读取得本次 Run/模型安全审计；保留原有未提交修改与数据库、配置摘要。
- [x] 在 tests/test_decision_contract_consistency.py 复现 Schema 可接受但后端拒绝的案例，以及安全错误降级问题；先观察失败。
- [x] 最小修改 discovery.py、model_schema.py、workflow.py 中共享规则与候选绑定；保持工具执行、人审和证据门独立。
- [x] Fake Provider 验证发现→人工审核→恢复→多轮只读工具→安全结束，非法类型/动作/引用均停止；只使用临时 SQLite。
- [x] 完整回归 581 项、SSH/Docker Mock 75 项、隔离 Docker＋Fake 2 项通过；前端构建通过；未调用真实 GPT、连接生产 SSH或创建正式 Run。
- [x] 活动工作为零后更新本地 API，核对健康、源码、配置与原数据库/Volume/资料保留。
- [x] 提交中文报告，区分已证实契约缺口与未保存的原始非法值。

操作者随后明确授权同步当前进度并推送 main；据此允许提交和普通推送，禁止覆盖远端已有提交。其余约束保持：不调用真实 GPT、不创建正式 Run、不连接生产服务器、不增加正式自动纠错、不猜测原始非法工具。
