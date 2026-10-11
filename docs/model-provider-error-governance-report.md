# ATR V2.2 模型调用稳定性与自主调查错误治理

验收日期：2026-10-10。实施目录：`E:\ATR`；分支：`fix/litellm-runtime-isolation`。继承原有独立 Python worker、硬超时和后台任务隔离。代码保持本地未提交状态，没有推送、合并、创建 PR 或修改远程 Tag。

## 1. 根因和边界

真实故障日志记录的是请求阶段的 `APIError / HTTP 529`。原分类将其记作普通连接失败，workflow 又只向调查传递包装类名 `ModelCallError`，同时生成离线候选。调查摘要固定显示 completed，无法区分任务收尾、模型成功和根因成立。

本地 Fake HTTP 已复现相同 worker → workflow → 数据库 → API 链路。529 现在归类 `provider_unavailable`。本轮没有访问真实中转站，仍无法判断原始 529 来自网关还是上游，也没有证据证明原失败账单的 6340+1500 Tokens 是实际用量。

另发现 LiteLLM 1.104.1 会把字符串、布尔和缺失 usage 转成整数或零。仅验证 LiteLLM 返回对象不能保证 actual 的真实性。本次在 OpenAI 兼容客户端的原始 HTTP 响应处验证计数字段，保留当前模型和 Base URL，不修改请求参数约定。

## 2. 529 处理前后

| 项目 | 修改前 | 修改后 |
|---|---|---|
| 错误类型 | provider_connection_error；UI 只有 ModelCallError | provider_unavailable；保留 APIError、request、529 |
| 调查 | completed；失败后产生离线候选 | unresolved；stage=stopped；model_status=failed |
| 已有结果 | 可能被离线候选覆盖 | 保留先前假设、反证、证据、只读工具记录 |
| 后续动作 | 旧调查路径可能继续使用离线提议 | 立即停止，不执行新增工具或请求 |
| 用量 | estimate 但没有关联诊断 | estimate/failed、费用未知、关联尝试与调查编号 |
| 重试 | num_retries=0 | LiteLLM 和 SDK 均为 0；界面说明人工重试可能收费 |

后台 job 的 completed 仅代表安全收尾函数完成；调查是否支持根因由 run.status 和证据门决定。模型成功另由 model_status 表示，三个字段不会相互代替。

## 3. 分类和安全审计

| 事实 | 分类 |
|---|---|
| HTTP 401、403 | provider_auth_error |
| HTTP 400、422 | provider_bad_request |
| HTTP 429 | provider_rate_limit |
| HTTP 500、502、503、504、529 | provider_unavailable |
| DNS、TLS、APIConnectionError、ConnectionError | provider_connection_error |
| 请求或外层硬超时 | model_timeout |
| 非 JSON | model_invalid_json |
| 字段、类型、引用、提议或 SDK 响应结构错误 | model_schema_error |
| ImportError、子进程启动失败 | model_import_error |
| 其他内部处理异常 | model_internal_error |
| 预算阻止请求 | model_budget_exceeded |

分类使用真实 HTTP 状态和确切异常类型/MRO，不匹配错误文本。以捕获的原始 HTTP 状态优先；未收到 HTTP 响应的传输异常不采用 LiteLLM 合成的 500/408。收到 200 响应头但正文超时仍归类 timeout，不能伪装成 JSON 错误。

现有用量行新增安全诊断 JSON，包含内部唯一 request_attempt_id、investigation_run_id、模型、provider、阶段、起止时间、总耗时、初始化/进程开销/网络/解析校验耗时、错误类别、原异常、HTTP、usage_kind/status 和 retry_count。事件使用同一关联编号。没有可测阶段耗时则记未知，不编造数值。

worker 额外发送两个不含请求内容的阶段标记，使外层超时能够区分初始化和请求挂起。强制结束或后端重启无法获知的实际结束时间保持空值；中断收尾同步诊断为 interrupted，并单独记录恢复处理时刻。

供应商请求编号仅来自允许格式的真实响应头，排除包含模型密钥的值；不把 completion ID 当请求编号。诊断不保存 Authorization、API Key、完整 Prompt、供应商错误正文或原异常字符串。安全栈帧只含文件名、行号和函数名。

## 4. 输出、授权和预算

新增严格 Schema：summary、hypotheses、next_action、action 必须存在；假设的目标、角色、因果链、预测、依据、证据引用、missing_evidence 和置信度必须合法。拒绝额外动作参数、未知候选动作、不存在的节点或证据引用。验证发生在 usage 成功结算之前。

Schema 失败保留可验证的原始 actual usage，但标记调用 failed；供应商未提供两个明确非负整数时只能 estimate。失败且无有效 usage 的费用保持 unknown，不将预留数量标作实付消耗。可信原始 usage 即使 SDK 解析失败也保留。

预留一次性释放，重复结算被拒绝。超出预留的有效 usage 触发 `provider_usage_exceeded_reservation`，后续请求被阻断；沿用操作员显式审核预算后清除的现有规则。中断请求保守按 estimate 收尾，不永久占用预留。

模型失败不会产生离线 GPT 候选，不更新已有假设生命周期，不进行自动付费重试。store 没有当前已确认绑定时无法读取；证据门、运行对象权限和现有只读工具策略保持有效。

## 5. 修改文件

| 文件 | 本轮作用 |
|---|---|
| backend/app/context/model_errors.py | 精确分类、安全诊断和原始 usage 校验 |
| backend/app/context/model_worker.py、model.py | 原始 HTTP 验证、阶段进度、追踪编号、时间审计 |
| backend/app/context/budget.py | 诊断关联、一次性结算、actual/estimate、中断收尾 |
| backend/app/storage/migrations.py、database.py | 迁移 23 和迁移前备份 |
| backend/app/agent/model_schema.py、workflow.py | 严格输出和错误传递；失败不生成离线候选 |
| backend/app/agent/business_investigation.py、api/v1.py | 业务与旧调查路径停止动作、保留历史 |
| backend/app/jobs.py、main.py | 异常和重启时同步用量终态 |
| frontend/src/model-error.tsx、main.tsx、business.tsx、styles.css | 中文错误、用量审计、合理轮询 |
| tests/test_provider_governance.py、test_model_isolation.py、e2e/test_model_errors.py | Fake HTTP/worker/API/浏览器验证 |
| tests/test_v22_jobs.py、test_v22_job_edges.py | 中断审计验证 |
| 现有调查/权限测试及 scripts/evaluate_faults.py | 给正常 Stub 补齐原本 Prompt 要求的必填字段；非法输出断言加强为失败且不执行工具 |

之前工作区中业务模型错误字段、隔离 worker、异步 jobs 和 compose 端口修改均保留。本轮 `.env`、模型名、Base URL、120 秒配置及 compose 文件内容未改动。

## 6. 界面效果和轮询

失败面板显示“模型服务暂时不可用”、gpt-6.1-sol、供应商请求、原始 APIError 和 HTTP 529；说明已有证据保留、没有额外未授权工具、未自动重试、人工重新调查可能收费。用量明示调用失败、Token 估算和费用未知；actual 不等于实际支付费用。

历史没有诊断字段时显示未知，不从日志反推事实。证据不足、权限拒绝和预算上限仍使用各自明确的未解决原因。job/run 轮询由 700ms 调整为 1500ms；终态停止轮询，保留实时轮次、证据和工具审计。浏览器验证终态后没有持续 job/run 请求。

截图：`test-results/governance/investigation-529.png`（隔离 Docker 的真实浏览器截图）。

## 7. 测试结果

最终完整回归：**245 passed，0 failed，0 errors，0 skipped**，357.98 秒。含真实 Chrome E2E；Ruff、锁文件检查、TypeScript/前端构建及 Docker 构建均通过。此前 233/242 项结果仅作迭代记录，最终结论使用本轮 245 项。

最终验证执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests --basetemp=test-results/tmp-governance-release-all --junitxml=test-results/governance/final-regression.xml --tb=short -q
.\.venv\Scripts\python.exe -m ruff check backend scripts tests
python -m uv lock --check
# frontend 目录
npm run build
```

覆盖正常 JSON、全部指定 HTTP 状态（额外含 422）、DNS/TLS/连接异常、请求正文超时、进程启动/挂起、无效 JSON/Schema、字段和引用错误、原始 usage 强制转换、防重复结算、超额预算、健康并发响应、529 全链路、反证保留、store 权限、人工恢复、旧记录及浏览器终态轮询。

治理测试先捕获 529/用量/进程问题；原始 HTTP usage 与中断审计补测也实际经历失败 → 修复 → 通过。复核发现的边界已补测试，包括 200 响应头后超时，以及 SDK 结构失败时保留有效 usage。Windows 未监听端口可能被环境转换为网关错误，因此连接异常的精确分类用独立 worker 中的确定性异常验证，不把随机网络结果当作假定事实。

## 8. Docker 和数据保留

独立项目 `atr-provider-governance-test` 使用单独目录数据库、合成密钥和 Fake Provider。API 只加入 internal 网络，无法访问真实中转站；浏览器通过独立 Web 入口验收。

最终 Fake 529 验收：API/Web 健康 200；API healthy；unresolved/stopped；APIError/request/529 完整保存；estimate/failed、费用未知、预留释放；2 条证据、2 次工具记录保留；store 读取 0；自动重试 0。恢复 Provider 后操作员发起新调查，得到 supported 和 actual usage。两次调查总共 2 次本地 Mock 请求。

最终测试通过后再次确认活动任务为 0，已保留原 `atr-runtime_runtime-data` Volume 更新本地 API/Web。API 容器 healthy；127.0.0.1:28000 与 127.0.0.1:28080 的健康接口均返回 200。更新前后逐表和逐文件摘要核对：Catalog-Test v12 approved、41 张业务表、8 个上传文件和 3 次历史调查原内容一致；全部历史调查和用量仍可通过受控 API 读取。`.env`、compose 文件及模型配置摘要完全一致。迁移 23 已安装，两份迁移前备份存在。

证据文件：`test-results/governance/docker-acceptance.json`、`final-regression.xml`、`before-deployment-checks.json`、`after-deployment-checks.json`。测试临时容器完成后停止；隔离测试数据保留用于复核。

## 9. 数据库迁移与回滚

迁移 23 仅给 model_usage_records 增加 `diagnostics_json TEXT NOT NULL DEFAULT '{}'`。旧行读取为空诊断；旧画像、调查、用量和失败账单不回写。保留现有 v21/v22 迁移及数据。

一致性备份：Volume 中 `/data/runtime.sqlite3.before-provider-governance-20261010.bak`；自动迁移前备份 `/data/runtime.sqlite3.pre-model-governance.bak`。原工作区文件另存 `test-results/governance/existing-work-before-governance.zip`。

回滚准备：旧 API 镜像 `atr-runtime-api:before-provider-governance-20261010`；旧 Web 资源从仍运行的容器导出并重建为 `atr-runtime-web:before-provider-governance-20261010`。原 Web 镜像历史层已被 Docker 清理，直接 tag/commit 不可用，故使用实际旧 HTML/JS/CSS 和 Nginx 配置保存恢复版本。

**旧 API 使用无列名 INSERT，不能直接写入含第 11 列的表。** 需要回滚时先等待所有任务结束，停止 API/Web，保留 Volume；使用 `test-results/governance/rollback_schema.py` 在离线数据库执行兼容回滚。脚本先完整备份新数据库并单独归档诊断，随后仅删除新增列和迁移 23 标记；业务、画像、调查、用量行均保留。离线复制库已实际验证回滚前后 41 张业务表摘要一致，旧 API 镜像在回滚后的复制库成功完成预算创建/预留/失败结算，外网关闭且模型请求为 0。之后用已保存旧镜像和同一 Volume 启动旧 API/Web。不要清空数据库、删除 Volume 或直接覆盖已产生新数据的数据库。

示例顺序（仅提供，本轮没有执行回滚）：

```powershell
docker compose stop api web
Get-Content -Raw test-results/governance/rollback_schema.py | docker run --rm -i --network none -e DATABASE_PATH=/data/runtime.sqlite3 -v atr-runtime_runtime-data:/data atr-runtime-api:latest python -
docker tag atr-runtime-api:before-provider-governance-20261010 atr-runtime-api:latest
docker tag atr-runtime-web:before-provider-governance-20261010 atr-runtime-web:latest
docker compose up -d --no-build api web
```

## 10. 限制和后续真实模型验收

- 本轮真实付费请求为 0，未完成真实 GPT 自主调查验收；Mock supported 不代表生产根因准确率。
- 原始 529 的网关/上游来源、是否收费和供应商请求编号仍无法确认，旧记录缺少的信息保持未知。
- 目前原始 usage 抓取针对现有 OpenAI 兼容调用；其他未验证 provider 无可信原始计数时采用 estimate。
- 外层强制结束或后端意外退出时不能恢复精确网络耗时/实际完成时间，记录未知并保守估算。
- 严格 Schema 可能揭示真实模型先前输出中被宽松处理的缺项；应根据安全错误修正兼容性，不能删除必填字段或放宽授权。

未来由操作员明确同意可能的额外费用后，在当前模型、Base URL、120 秒超时配置下人工启动一次 Catalog-Test 调查。检查本轮内部尝试 ID、供应商后台真实记录、HTTP、原始 usage、错误阶段、store 授权边界及证据门；失败时停止，不连续自动尝试。需要扩大 store 范围应先按现有授权流程人工确认。

## 最终状态

```text
IMPLEMENTATION_STATUS=completed
TEST_RESULT=245 passed; 0 failed; 0 errors; 0 skipped
DOCKER_HEALTH=API healthy; 28000/28080 health HTTP 200
MOCK_529_HANDLING=passed; unresolved/stopped; no automatic retry; manual recovery supported
MODEL_ERROR_UI=passed; Chinese cause/stage/APIError/HTTP529 and retry fee notice
DATABASE_COMPATIBILITY=passed; migration23; Catalog-Test v12 and all historical data preserved
REAL_PAID_REQUESTS=0
GIT_PUSHED=false
```
