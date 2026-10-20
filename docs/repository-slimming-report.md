# ATR 仓库精简报告

日期：2026-10-20

本次精简按奥卡姆剃刀原则处理：删除不参与运行主链的历史记录、生成结果、一次性验收脚本和孤儿测试；保留所有当前运行能力及其必要的安全边界。

本次变更删除 **116 个文件、约 193,000 行历史/脚手架内容**；保留的提交后仓库有 425 个 Git 跟踪文件。

## 已删除

- V2.1/V2.2 历史架构、验收、评测、迁移和一次性审计文档。
- `docs/verification/` 和旧的 XML/JSON 测试结果。
- A/B 实验、旧网关升级、旧密码验收和 Docker Demo 验收脚本。
- 只服务于上述脚本的集成夹具、隔离验收文件和孤儿测试。
- Archify 仅用于展示的示例图、示例多语言文件和示例校验条目；保留结构图渲染器、运行时依赖、许可证和完整性清单。
- 本地未跟踪的 pytest 临时目录及旧交接草稿。

## 保留的运行核心

- `backend/app/business/`：资料上传、静态解析、源码索引和业务画像。
- `backend/app/system/`：业务图、范围发现、映射审核和授权边界。
- `backend/app/agent/`：调查规划、Decision Runtime、事实报告、Evidence Gate 和任务停止策略。
- `backend/app/connectors/`：Mock、SSH、固定只读工具和 Docker 读取策略。
- `backend/app/runtime/`、`backend/app/context/`、`backend/app/storage/`：任务执行、模型隔离、预算、审计和 SQLite 持久化。
- `backend/app/api/`、`frontend/src/`：现有 API 和完整工作台页面。
- `collector/`、`demo/`、`scripts/ssh_gateway.py`、`scripts/run-local.*`、`scripts/package_release.py`：可选 Collector、隔离 Demo、服务器只读入口、本地启动和发布打包。
- `third_party/licenses/`、Archify 结构图运行时和所需 Node 运行时。

## 测试策略

保留当前后端、Runtime、模型隔离、SSH 协议、证据安全和浏览器流程测试。删除的测试只覆盖已删除的一次性验收脚本或历史实验，不影响运行时功能。

精简前完整基线为 **1093 passed / 10 failed**；失败已存在于精简前，集中在预算、模型兼容、映射边界和 SSH 测试。精简后的验证会分别记录收集错误、构建错误和既有失败，避免把基线问题归因于删除。

## 维护边界

新增功能应先进入当前 `backend/app`、`frontend/src` 和对应的最小回归测试。历史验收输出放在本地 `test-results/`，不再提交到 `docs/`；一次性实验应在仓库外保存，避免重新形成产品脚手架。
