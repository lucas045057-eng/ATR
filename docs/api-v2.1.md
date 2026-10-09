# V2.1 API 清单

原 `/api/v1` API 保留。除 GET `/api/v1/health` 外，以下接口要求 `Authorization: Bearer <OPERATOR_TOKEN>`。请求采用严格 Pydantic Schema，未知字段拒绝。本文路径省略 `/api/v1`。本地默认 http://127.0.0.1:8000。

## 新增接口

| 方法与路径 | 输入/输出用途 |
|---|---|
| GET /businesses | 业务清单、当前状态与覆盖 |
| POST /businesses | BusinessCreate `{name,description}`；201，返回初始画像含 business_id/system_id |
| GET /businesses/{id}?version= | 最新或指定历史画像 |
| GET /businesses/{id}/versions | 版本、摘要、时间列表 |
| GET /businesses/{id}/artifacts | 上传资料状态、slot、hash、错误 |
| POST /businesses/{id}/artifacts?filename=&mode=&slot= | 原始文件字节流；mode=append/replace，slot 默认 source；202 job_id/artifact_id |
| DELETE /businesses/{id}/artifacts/{artifact_id} | 逻辑移除，发布新画像；历史文件不物理删除 |
| POST /businesses/{id}/analyze | 预算内模型理解；202 job_id，静态结果保留 |
| POST /businesses/{id}/review | BusinessReview，版本/事实/角色/冲突审核 |
| POST /businesses/{id}/code/search | CodeSearch，FTS5 元数据检索 |
| POST /businesses/{id}/code/context | CodeContext，校验版本摘要的局部代码 |
| POST /businesses/{id}/files/{file_id}/media-review | `{text,expected_version}`，图片/PDF 人工录入 |
| GET /businesses/{id}/budgets | 限额/累计量/usage/停止原因 |
| PUT /businesses/{id}/budgets/{budget_id} | BudgetLimits 完整限额，保持累计用量 |
| GET /model/capabilities | 配置声明、模型窗口、JSON、usage 策略；vision adapter pending |
| GET /jobs/{job_id} | queued/running/completed/failed/interrupted、result、error |
| GET /businesses/{id}/targets | 关联目标，仅含凭据引用及主机公钥，无私钥 |
| POST /businesses/{id}/targets | RuntimeTargetCreate；201 |
| POST /businesses/{id}/targets/{target_id}/discover | 轻量发现；202 job_id |
| GET /businesses/{id}/discoveries | 最近 20 次发现、匹配依据及未匹配项 |
| GET /businesses/{id}/bindings | 所有候选、分数、原因、证据 ID、审核状态 |
| POST /businesses/{id}/bindings/{binding_id}/review | `{decision:confirm/reject,reason}`；审核范围、发布图谱 |
| POST /businesses/{id}/targets/{target_id}/tools | `{tool_id,arguments}`；固定工具，202 job_id |

## 请求示例

以下 PowerShell 使用自己的操作员令牌，不把令牌保存到项目：

```powershell
$atrBase = 'http://127.0.0.1:8000/api/v1'
$atrHeaders = @{Authorization = 'Bearer <OPERATOR_TOKEN>'}
$atrBusiness = Invoke-RestMethod -Method Post -Uri "$atrBase/businesses" -Headers $atrHeaders -ContentType 'application/json' -Body '{"name":"Catalog","description":"商品目录测试业务"}'
$atrUpload = Invoke-RestMethod -Method Post -Uri "$atrBase/businesses/$($atrBusiness.business_id)/artifacts?filename=catalog.zip&mode=replace&slot=source" -Headers $atrHeaders -ContentType 'application/octet-stream' -InFile 'C:\authorized\catalog.zip'
Invoke-RestMethod -Uri "$atrBase/jobs/$($atrUpload.job_id)" -Headers $atrHeaders
```

上传不是 multipart；filename 为上传文件名，不能带目录。等待任务 completed 后获取业务最新 profile_version 再审核。超过上传限额为 413，类型/参数/版本错误为 422，不存在为 404，范围未授权为 403，活动任务冲突为 409。

BusinessReview：

```json
{
  "expected_version": 2,
  "subject_id": "profile",
  "decision": "approve",
  "reason": "已核对服务与依赖骨架，允许受限只读调查"
}
```

subject_id 可以是 profile、事实 id、`service:<name>`（set_role）或冲突 id（resolve_conflict）。decision 为 approve/reject/set_role/resolve_conflict，角色字段为 application/proxy/datastore/cache/worker/unknown。reason 至少 5 字符；每次操作后使用返回的新版本。

CodeSearch 与 CodeContext：

```json
{"keyword":"record","module":null,"limit":20}
```

```json
{"file_id":"<source-reference-id>","symbol":"record","start_line":1,"max_lines":60,"profile_version":2}
```

search 当前有效索引，limit 还受 CODE_SEARCH_LIMIT 限制。context 无任意路径参数，必须指定属于该业务画像版本的 file_id；最多 160 行。返回 digest、版本、行号、脱敏文本和字节数。

Mock 目标（测试夹具，不是真实服务器）：

```json
{
  "name":"隔离 Catalog",
  "connector":"mock",
  "allowed_paths":["/srv/catalog"],
  "mock_scenario":"mixed",
  "docker_reads_enabled":false
}
```

SSH 目标必须另填 host/port/username/credential_ref/host_public_key，access_authorized=true、permission_boundary=forced_command_gateway、boundary_attestation 至少 10 字符，且本地 SSH_ENABLED 已启用。max_calls 默认 100、timeout_seconds 默认 10、max_output_bytes 默认 48,000。凭据文件映射由操作者 .env 提供，禁止私钥文本。服务器管理员配置见 [安全边界](security-boundaries.md)。

固定工具参数：

| 工具 | arguments |
|---|---|
| host.info/memory/disk、network.listeners | `{}` |
| process.list、service.list、container.list | `{limit:60}`，1–100 |
| service.status | `{name:"catalog-api"}`，须 confirmed |
| service.logs | `{name:"catalog-api",tail:80}`，tail 1–200，须 confirmed |
| container.status/logs | `{container_id:"<hex-id>",tail:80}`，tail 仅 logs，须 confirmed 和 Docker 授权 |
| config.list/read | `{path:"/srv/catalog/config"}` 或具体授权文件，清单单层 |

工具 task 的 completed 表示执行封装已结束；业务读取可能仍返回 `{status:"denied"/"failed"/"timed_out",observation_id,error}`，不能仅看 job completed 当作成功证据。成功返回 observation_id/payload/bytes/observed_at/source_type/connector。

## 原调查接口的变化

POST `/incidents` 仍使用 `{system_id,symptom,window_start?,mode?}`；mode 支持 normal/graph_only/graph_pattern/alert_only。POST `/incidents/{id}/runs` 现在返回 **202 `{job_id,status}`**，轮询 `/jobs/{job_id}`，完成后用 job.result.id 调用 GET `/runs/{id}` 获取步骤、实际工具、当前证据、hypotheses.evidence_links、budget 与未解决原因。

V2 业务需 approved 档案和 confirmed 范围，否则不执行业务实时读取。模型缺失输出 offline/unresolved。原 `/systems`、图谱、`/patterns`、修复/审核、metrics 和配对评估接口继续使用原流程，证据 Gate/Trusted 条件已加强。完整请求 Schema 可从本地 OpenAPI 定义读取，但未开放交互 Swagger 页面。

同一业务任务锁和 expected_version 保护并发。后端重启任务 interrupted，不自动重试。版本/预算恢复规则见 [架构](v2.1-architecture.md) 与 [预算](context-budget.md)。
