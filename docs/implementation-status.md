> 历史 V2.1 文档：以下数量与结论保留原记录。当前 V2.2 的实际结果请见 [验收报告](v2.2-acceptance-report.md)，启动见 [部署](deployment.md)。

# V2.1 实现状态

本版为上游 ATR 的增量实现；原四页、Demo、LangGraph、LiteLLM、SQLite、图谱/Pattern 保留。2026-10-09 本机最终测试 **67 passed，0 failed，0 skipped**；前端构建、Ruff、依赖锁和 Compose 配置检查通过。详情见 [验收报告](acceptance-report.md)。

## 已实现并通过离线/隔离测试

- 流式上传、ZIP/TAR 安全解包、限资源 worker、静态清单/AST/配置/hash、异步任务与中文接入。
- 五类来源业务画像、持久审核/角色/冲突、版本对比、哈希复用与 stale 知识保留。
- SQLite FTS5 元数据搜索、校验摘要/业务/版本的局部代码、初始不发送源码全文。
- 独立 Context Budget Manager、原子预留、硬停止/增额恢复、actual/estimate usage 和费用标识。
- 固定工具、SSH pinned host key 与 JSON 固定入口、失败审计、显式 Mock、轻量范围候选及人工确认。
- 原图谱融合与五类真实 provenance、运行冲突双方保留、等价结构保留 Trusted 资格。
- 已审核业务与范围驱动 LangGraph 调查，模型候选选择/Policy 执行、当前证据 Gate、假设支持/反证/预算可见。
- 修复记录 + 真正回环 HTTP GET 验证 + 人审之后 Trusted 的控制测试；不会自动修复或提升。
- 真实本地 API/Chromium 完整接入 E2E、回环 Paramiko SSH 协议测试，未使用浏览器后端响应替身。

## 部分能力与已知限制

- 图片/PDF 独立接口及人工录入已实现，自动视觉/PDF 理解适配尚未实现。
- Python AST 和有限 JS/TS 正则提供骨架，动态语义、完整别名/调用链解析不完整；FTS 仅索引路径/符号/导入/路由。
- 文件解析增量复用，画像骨架本地重建；不是完整依赖影响增量计算器，也不保证每次只评估绝对最小子图。
- 普通过程仅读名称摘要；调查主要选择已批准状态/日志及按需源码，不提供自由进程深读/通用无限 Agent。
- Evidence Gate 检查范围/时间/信号/重复/冲突/解释，不能证明任意自然语言因果结论正确。
- 模型能力为操作者声明；费用是配置费率估算或 unknown，没有供应商自动价格/账单核对。
- 删除为逻辑停用，历史/失败资料仍保留，无自动物理清理。API 只支持单进程本地任务池和单操作员令牌，无多租户/RBAC。
- SSH 安全依赖管理员正确强制命令/账号/文件权限，声明不能替代实际服务器权限审计。原 Collector 仍有 daemon 权限，只用于显式本地 Demo。

## 未真实验证

Docker Engine 未运行，未构建/启动容器或完成 A/B 真故障修复；无真实模型 API/计费调用；无真实授权 Linux 业务服务器，未执行 Linux 网关 openat/systemd/journald 或完整 SSH 账号隔离；WSL2 实际启动未运行验收。

测试 stub 只验证响应/usage 接口与 Gate 分支，Mock 只验证受控范围/证据链。未测真实业务准确率、成功率、跨系统性能改善，不展示虚构数字。

下一阶段首先做明确授权的 Linux 只读账号与网关验收，再接真实模型和容器 Demo；随后完善媒体解析、语言语义索引、资料保留/清理及更多业务用例评估。
