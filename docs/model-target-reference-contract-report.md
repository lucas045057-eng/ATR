# ATR V2.2：合法服务节点引用契约修复报告

本轮仅实施方案 A：在每次模型决策输入中提供合法节点候选及精确引用规则，并统一候选与后端校验集合。没有增加自动纠错请求，没有修改供应商接口、推理配置、预算上限或业务数据。

## 修复内容与范围

| 文件 | 修改 |
| --- | --- |
| `backend/app/agent/model_schema.py` | 新增 `allowed_service_nodes()`，统一提取当前 Run 图中 `kind=service` 且角色不是 `runtime` 的节点；容量为 30；假设与验证预测共用此集合。 |
| `backend/app/agent/workflow.py` | 每轮动态增加 `allowed_hypothesis_targets` 候选表，包含服务名称、精确 `node_id`、角色；删除静默截取前 30 个节点的行为；明确引用契约。 |
| `backend/app/context/model_errors.py` | 允许安全诊断携带候选数量及容量两个内部计数字段；不增加原始响应或 Prompt 存储。 |
| `frontend/src/model-error.tsx` | `unknown_target` 显示“不在本轮允许引用的服务节点集合中”；超过候选容量时显示数量、上限及“模型请求未发送”。 |
| `tests/test_target_reference_contract.py` | 新增节点类别、标识符替换、角色、跨业务及目标、容量、压缩、多轮安全停止的回归与 Fake Provider 验收。 |
| `tests/e2e/test_model_errors.py` | 补充真实浏览器对非法节点字段级错误的展示与敏感值不回显验收。 |

生产实现没有硬编码随想记节点。提供的历史节点 ID 仅用于回归夹具。`kind=service, role=datastore` 的数据库服务合法；单独 `kind=datastore` 的图节点仍不能充当服务假设目标。

## 新旧 Prompt 与集合一致性

| 项目 | 修改前 | 修改后 |
| --- | --- | --- |
| 合法节点说明 | 要求使用 `system_graph` 的精确服务 ID 和匹配角色，但没有单独候选表及禁止替代规则。 | 明确引用 `allowed_hypothesis_targets`，逐字复制其中的 `node_id`，假设角色必须等于所选候选角色。 |
| 容易混淆的标识符 | 没有逐项区分。 | 禁止用服务名称、容器 ID、SSH Target ID、动作 ID、假设 ID、其他图节点类型的 ID 替代；禁止生成 UUID。 |
| 无法确定目标 | 缺少明确的引用处理说明。 | 返回空假设，不编造引用。证据不足但目标已知时仍按现有规则报告 `unresolved`。 |
| 引用集合 | Prompt 过滤服务及非 runtime 角色后取前 30 个，校验器检查全部服务节点。 | 同一 `allowed_service_nodes()` 同时供 Prompt 与校验器使用，没有两套独立过滤逻辑。 |
| 多轮压缩 | 保留图节点，但没有独立合法引用表。 | 每轮重新从 Run 图生成候选；压缩只缩短证据文本及消除重复历史，保留完整候选 ID、名称、角色。 |

超过 30 个有效服务节点时，完整集合检查触发 `target_candidate_limit`，在调用供应商、预留模型用量之前停止。不会随意丢弃第 31 个服务，也不会提高 Token 配置；操作者需要审核业务调查范围。30 个以内的请求仍须通过原有单次输入预算检查；不可压缩到预算内的请求继续安全拒绝。节点候选是引用契约，并不授予工具权限。

输入增加量受到候选容量和既有输入预算双重限制。以回归案例的两个节点计算，新增规则文本为 389 个 UTF-8 字节，候选表紧凑 JSON 为 205 个字节；这些是本地字节统计，不是供应商计费 Token，真实模型用量本轮未测量。

## 验收结果

- 全量回归：**454 passed，847.80 秒（14 分 07 秒），0 失败、0 错误、0 跳过**。包含 19 项新增节点契约案例、原有安全回归及浏览器检查。
- 前端：`npm run build` 通过；实际浏览器确认 `unknown_target` 显示精确字段路径和新的中文说明，原始非法值、密钥及额外字段不回显。
- 代码检查：`ruff check .`、`git diff --check`、`uv lock --check` 通过；没有修改依赖或锁文件。
- 本地 API / Web Docker 镜像构建通过。以下隔离 Docker Mock 验收也全部通过。

独立 Docker Mock 使用本轮构建的 API 镜像，`--network none`、只读根文件系统和临时 `/tmp` 数据库，未挂载业务运行卷。容器内 Fake Provider 接收 5 次请求：API 合法引用 1 次、DB 合法引用 1 次、错误角色 1 次、第一轮合法第二轮非法 2 次。最后一个场景的错误准确定位 `hypotheses[2].target_node_id / unknown_target`；只执行已批准的前三次 Mock 工具，第二轮附带的后续动作没有执行。31 个服务节点在请求前被拒绝，供应商请求计数不增加。所有模型请求自动重试数为 0。

回归覆盖服务名称、容器 ID、SSH Target ID、动作 ID、假设 ID、非服务 datastore 节点、runtime 节点及自行编造的标识符；合法 ID 与错误角色的组合仍被拒绝。现有证据引用、Evidence Gate、动作白名单、SSH 主机公钥、凭据有效期、Docker 权限、授权映射、Run 目标绑定和预算检查沿用原实现。

测试证据：`test-results/target-contract-full.xml`、`target-contract-full.log`、`target-contract-docker-acceptance.log`、`target-contract-docker-build.log`；浏览器截图位于本轮隔离测试目录。这些目录不参与 Docker 构建、不上传 Git。

## 本地更新与数据保留

已更新 `atr-runtime-api-1` 与 `atr-runtime-web-1`。API 容器为 **healthy**；`127.0.0.1:28000/api/v1/health` 和 Web 代理 `127.0.0.1:28080/api/v1/health` 均返回 **HTTP 200**，首页返回 200。容器内三个修改过的后端文件与本地源文件 SHA-256 一致。

在已部署代码中，以历史 Run 的图快照进行只读候选查询，实际得到 api、db 两个合法 service 节点及其准确角色；没有把另一个 datastore 节点加入集合，没有发起模型或服务器调用。

更新前后 **42 张表的行数及数据摘要全部一致，405 个上传资料文件的摘要一致**。保留 1 个业务画像、4 个服务器目标、28 条服务映射、8 个历史 Run；数据库完整性检查均为 `ok`。`.env` 文件和容器完整环境变量摘要均不变。发现 `.env` 与运行容器中的两项预算值不同，因此更新时仅以临时内存 Compose 覆盖保持原运行值，没有改写文件或预算。

继续挂载原有 `atr-runtime_runtime-data` 卷；`/demo`、`/repos` 仍指向同一 Windows 目录且保持只读。Docker Desktop 重建后将绑定源路径显示为其 Linux 映射路径，已按同一目录规范化核对，挂载属性一致。重启前确认没有活跃调查、模型请求、工具、发现或上传任务；没有中断历史工作。保留验证及源文件匹配证据位于 `test-results/target-contract-preservation-before.json`、`target-contract-preservation-after.json` 和 `target-contract-deployment-verification.json`。

没有数据库迁移或历史记录修正。更新前后通过 SQLite 只读连接计算各表的行数与摘要；仅保存计数及哈希，不输出凭据、敏感日志或完整模型内容。业务运行卷未删除或重建。测试数据使用独立临时数据库。

## 下一次真实 Run 操作

1. 刷新本地 ATR 页面。由于 API 重启会清除内存会话密码，在“服务器范围”重新为 `suixiangji安全网关2` 提供短期密码凭据；确认主机公钥及既有授权，不扩大范围。
2. 在故障调查页明确选择该目标 `d997a5d5-8898-4f99-bbe7-765e38facac9`，确认 API、DB 两个既有映射仍为已确认状态，再由操作者手动创建新 Run。
3. 核对新 Run 的目标绑定与图快照，关注两轮模型审计：合法引用通过；若模型仍输出非法引用，应显示安全字段路径、`unknown_target` 和新的中文说明，后续工具必须停止。
4. 如遇 `target_candidate_limit` 或单次输入预算拒绝，根据提示人工审核调查范围或预算，不自动重试，不重写既有审核决定。

本轮没有执行以上真实服务器验收。历史 Run `bda40379-0a11-415c-86b8-746e252baed8` 的失败原始响应未保存，仍不能确定其具体非法值或生成原因；本次合成测试不冒充历史响应复现。Prompt 能提供更明确的规则，但不能保证真实模型永远遵守；严格校验继续拒绝全部非法引用。

真实付费模型调用：0；生产服务器连接：0；真实 SSH/Docker 工具：0；Push / Merge / PR：均未执行。
