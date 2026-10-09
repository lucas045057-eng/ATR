# V2.1 基线审计与变更记录

基线仓库 lucas045057-eng/ATR，main 提交 `73f3603a1df633d7bffe2d9d98b411b6a1e9bc68`。先审查后端、Collector、React、SQLite、文档和两个 Demo，再分阶段增量扩展，没有重建技术栈或替换上游提交历史。

## 可复用部分

复用 system 下 Compose/源码/图谱建模及快照；保留 SQLite 原故障/运行/Pattern/评估表与 API、固定 Collector 工具、LangGraph planner/reviewer、LiteLLM 和 Evidence Gate；保留 React 四页、知识审核与双系统手工 Demo。缺口为无上传画像、源码版本检索、独立预算、SSH/范围确认、后台任务及自动 E2E。

## 要求核查的十项

| 问题 | 基线结论与修复 | 回归证据 |
|---|---|---|
| Docker Titles/Processes | 把 list 行当 dict；改为标题 zip 并保留 dict 兼容 | test_regressions 的 Docker 行解析 |
| signals_available 循环复用 | 匹配记录引用后一次角色/可用信号；按 Pattern 单独保存 | per-pattern state 回归 |
| Gate 重复/弱相关 | 增加目标/有效依赖、当前版本/时间、内容去重、工具族、失败信号、因果解释、反证与冲突限制 | baseline Gate 正反例、investigation |
| 敏感配置外发 | 递归秘密键、含空格值、JSON、凭据 URL 等脱敏；默认关闭外部模型，摘要/局部限定 | redaction、budget/no full source、private key |
| Collector 权限 | Docker socket 仍是高权限，无法用 :ro 消除；Collector 改为显式 docker-demo，默认离线无 socket；降 capabilities/只读容器 | Compose 配置验证；真实 daemon 未验收 |
| 角色依赖名称 | 从镜像/构建线索识别，未知保留 unknown，允许人审角色，不以 db 子串猜测 | baseline role、profile role |
| 图谱过度使 Pattern stale | 用语义结构判断等价，匹配仍按当前拓扑/工具；格式变化不废弃 Trusted | equivalence/format-only 回归 |
| Compose 固定服务数 | 解除八服务接入限制，1–200 服务，结构/名称/文件量边界 | 12 服务 + 原 Demo 解析 |
| 缺少 E2E | 增加真实 Chromium/本地 API 全链测试和协议/安全/预算集成 | tests/e2e/test_browser.py |
| 长期同步调查 | 202 job + UI 轮询，保留历史与任务错误；后台重启记录中断 | API investigate 与浏览器流程 |

Gate 仍不能证明自然语言因果全正确；角色识别是静态线索；等价测试不代表任意 Pattern 都适用。未用测试 stub 宣称真实模型质量。

## 阶段记录

- Phase 0：上述基础缺陷、原 Demo 本地图谱、Collector 行、脱敏及 Gate 回归。
- Phase 1：原始流上传、安全 ZIP/TAR、受限 worker、文件清单/hash/type/static，中文接入。
- Phase 2：严格画像与来源、历史/审核/失效知识、FTS5/局部源码、增量解析、独立预算和结构化模型接口。
- Phase 3：固定 Mock/SSH、pinned key、JSON 固定入口、策略/审计、轻量发现与范围审核；隔离回环协议验证，真实 Linux 尚需授权验收。
- Phase 4：复用图谱融合、冲突双方、当前映射、Pattern 建议、LangGraph 与证据链/预算，受控故障测试。
- Phase 5：全量自动测试、真实浏览器、前端/质量/锁/Compose 配置检查及本版文档。

测试在开发阶段持续运行，最终统一记录在 [验收报告](acceptance-report.md)。本机无法启动 Docker Engine，未访问外部业务服务器或真实模型，阶段对应运行验收仍有待办。

## 兼容变化

运行启动 POST 从同步 200 改为 202 job，原客户端需适配轮询；本版 UI 已适配。默认 Compose 不启动 Collector，原 Demo 须显式 docker-demo。原 SQLite 表不删，新增 schema 21 和启动前一致备份。原 fact_claims CHECK 保留，真实五类来源通过 provenance/source_type 扩展。生产镜像前端改用 npm ci 尊重 lock；镜像实际构建未验收。

没有新增自由命令执行器、图数据库、向量库、自动修复、自动服务器安装或多 Agent 服务。
