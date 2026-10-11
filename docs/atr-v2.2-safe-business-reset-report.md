# ATR V2.2 测试业务数据清理与初始化报告

执行日期：2026-10-10，Asia/Shanghai。项目：`E:\ATR`。最终复核时间：2026-10-10T23:05:30.873898+08:00。

## 1. 结果与最新授权

当前工作库已恢复到全新接入状态。Business、System、Incident、Diagnosis、上传索引、运行观测、证据、历史经验、Pattern、业务 Token 统计和任务记录均为空。当前 API/Web 返回 HTTP 200，实际浏览器可打开“新业务接入”。未向工作库重新添加测试业务。

操作者在执行期间明确更新要求：**“测试业务相关删除就行不用备份保留”**。本轮据此直接删除确认归属于 Catalog-Test 的数据库、上传资料和同卷历史数据库备份；不归档业务数据，也不新建备份。先前创建的空备份目录已经移除。最初任务书的备份、恢复演练和完整数据回滚要求因此不再执行，不能将本轮称为“已验证备份可恢复”。

## 2. 清理前环境与范围审计

- API 容器：`atr-runtime-api-1`，运行且 healthy；Web：`atr-runtime-web-1`，运行。
- API 镜像：`atr-standard-ssh-api:20261010`，`sha256:73cdb72f439a3c7f124ee2c036a3cc806bbe02c2bb2fbbda58d7ae8b518235ee`。
- Web 镜像：`atr-standard-ssh-web:20261010`，`sha256:549ee9f28f6114e4d9ef34245b83b786e474e19839f5d97c2d5b4f61aec7171c`。
- 实际 Compose：`E:\ATR\compose.yaml` 与 `E:\ATR\test-results\standard-ssh\deployment-compose.yaml`。
- API/Web 端口：`127.0.0.1:28000` / `127.0.0.1:28080`。
- 实际 Volume：`atr-runtime_runtime-data`；Docker Linux 路径：`/var/lib/docker/volumes/atr-runtime_runtime-data/_data`；容器挂载点：`/data`。Docker Desktop 管理该 Linux 路径。
- 实际工作库：`/data/runtime.sqlite3`；资料根：`/data/artifacts`；SQLite 为 WAL 模式。清理前主库 WAL 为 0 字节，SHM 为 32768 字节；历史数据库的 WAL/SHM 附属文件也识别并纳入精确范围。
- 枚举全部容器挂载后，仅当前 API 使用该 Volume，无其他实例共享它。其他项目和隔离测试容器未重启或删除。
- 通过 `sqlite_master` 动态审计结构、字段、索引和外键，并与当前镜像执行 `Database.initialize()` 产生的独立新库比较。实际为 42 张表，结构摘要完全一致；不是依据旧报告猜测表数。
- 迁移版本：21、22、23。表中没有独立平台设置或凭据配置表；模型、SSH、安全参数由现有环境配置管理，无需复制业务数据库中的持久设置。
- 主工作库 `integrity_check=ok`、`foreign_key_check=[]`。queued/running jobs、运行中的调查/发现、排队资料、pending 工具/观测/模型记录均为 0。
- 唯一业务及唯一系统均为 Catalog-Test，业务 ID：`b587d2c5-80e1-4f7c-8e79-b2d363b2d247`。共 1 个业务、5 个事件、6 次调查、13 个画像版本、192 个业务事实、15 份图谱快照、7 条模型用量；Pattern 和 Experience 原本均为 0。
- 全部上传资料共 8 个物理文件；1 个原始上传对象及 5 个数据库登记的源码文件均校验大小和 SHA256。其余 manifest/reuse 文件一并纳入物理文件摘要。
- 同卷 4 份历史 `.bak` 均通过完整性及外键检查，只含同一 Catalog-Test 业务和系统，没有平台配置表。

## 3. 实际清理步骤

1. 保存安全审计元数据、逐表记录数、行集合摘要、结构/外键、文件 SHA256 和运行元数据。审计证据不包含数据库原始业务内容、Prompt、API Key 或密码。
2. 记录现有 Git 分支、HEAD、工作树状态和 48,416 个既有源码、文档、脚本、测试及测试结果文件的摘要。
3. 确认无活动任务、无共享 Volume；停止当前 API/Web，确认二者均处于 exited 状态，阻断写入。
4. 使用**同一个当前 API 镜像 ID**运行一次性本地维护容器，网络设为 `none`，不传入模型密钥，不调用 SSH 或模型。
5. 用应用正式 `Database.initialize()` 先在同卷临时路径创建新库，验证完整性、外键、迁移、结构及全部初始表计数与独立基准一致；完成 WAL checkpoint 并关闭连接。
6. 只有上述检查通过后，才删除精确定位的旧工作库、相关 WAL/SHM、`/data/artifacts` 下的 8 个文件，以及 4 份已确认归属的数据库备份和附属文件。没有执行大量手工 DELETE，没有删除 Volume。
7. 将已验证的新库置于原 `/data/runtime.sqlite3`；创建空 `/data/artifacts`，所有者恢复为运行账号 UID/GID 10001。旧 WAL/SHM 不与新库混用。
8. 启动原 API/Web 容器，保留容器 ID、镜像、环境变量、端口、挂载、网络和 Compose。
9. 对实际页面、API、FTS、当前库及隔离 Mock 场景验收；完整回归使用独立临时库。

删除的同卷数据库备份：

- `runtime.sqlite3.before-litellm-fix-20261010.bak`
- `runtime.sqlite3.before-output-compatibility-20261010.bak`
- `runtime.sqlite3.before-provider-governance-20261010.bak`
- `runtime.sqlite3.pre-model-governance.bak`

清理前守卫曾分别因未纳入的历史备份附属文件、新库只读检查产生的空 WAL/SHM 暂停；检查并补齐对应处理后才执行删除。暂停期间旧业务数据未删除。相关失败日志保留，不作为成功验收。

## 4. 全表清理前后统计

| 实际表名 | 清理前 | 清理后 |
|---|---:|---:|
| `artifact_files` | 5 | 0 |
| `business_fact_claims` | 192 | 0 |
| `business_profile_versions` | 13 | 0 |
| `business_profiles` | 1 | 0 |
| `business_reviews` | 8 | 0 |
| `context_budgets` | 7 | 0 |
| `diagnosis_runs` | 6 | 0 |
| `diagnostic_steps` | 10 | 0 |
| `discovery_runs` | 1 | 0 |
| `evaluation_cases` | 0 | 0 |
| `evaluation_trials` | 0 | 0 |
| `events` | 36 | 0 |
| `evidence` | 15 | 0 |
| `experiences` | 0 | 0 |
| `fact_claims` | 182 | 0 |
| `graph_conflicts` | 0 | 0 |
| `graph_edges` | 101 | 0 |
| `graph_nodes` | 140 | 0 |
| `graph_snapshots` | 15 | 0 |
| `hypotheses` | 9 | 0 |
| `hypothesis_evidence` | 20 | 0 |
| `incidents` | 5 | 0 |
| `jobs` | 14 | 0 |
| `model_usage_records` | 7 | 0 |
| `pattern_matches` | 0 | 0 |
| `pattern_versions` | 0 | 0 |
| `reviews` | 0 | 0 |
| `runtime_observations` | 20 | 0 |
| `runtime_targets` | 1 | 0 |
| `schema_migrations` | 3 | 3 |
| `service_bindings` | 1 | 0 |
| `source_index` | 3 | 0 |
| `source_search` | 3 | 0 |
| `source_search_config` | 1 | 1 |
| `source_search_content` | 3 | 0 |
| `source_search_data` | 3 | 2 |
| `source_search_docsize` | 3 | 0 |
| `source_search_idx` | 1 | 0 |
| `step_evidence` | 20 | 0 |
| `systems` | 1 | 0 |
| `tool_calls` | 15 | 0 |
| `uploaded_artifacts` | 1 | 0 |

`schema_migrations` 的 3 行是迁移元数据；`source_search_config` 的 1 行和 `source_search_data` 的 2 行是当前初始化器创建空 FTS 时产生的内部元数据。它们与独立空库一致，不是旧业务知识；FTS 主表、content、docsize、idx 均为空。

## 5. 摘要与一致性结果

- 原工作库文件 SHA256：`ec2047128dcb27bb3e9ada3d08a4b4eb0f5ad5842efbe27eb146041f9b8bd6b1`。
- 新工作库文件 SHA256（启动后审计时）：`d0881891af59531a9a09d1e756c6b4f89272347d0075995258f456dcf71c6885`。
- 新旧数据库结构摘要：`715d80c9223f464f02c6b8412de638bfea3631e6bd01fbd20627a2819517ea57`，与当前初始化器一致。
- `.env` SHA256：`db506425b319d007e8b1712abf2c486af6d587377a546a53c053c14e9dd99dfb`，执行前后完全一致；仅记录摘要，未复制或显示密钥。
- 最终复核再次证明：完整回归和隔离验收前后，全部 42 张工作库表的计数与行摘要保持一致，没有测试数据回灌。
- 全部旧资料和已删除文件的逐文件摘要：`test-results/safe-business-reset-20261010-224748/initialization-verification.json` 的 `deleted_files_sha256`。这只是校验元数据，不能用于恢复原文件。
- 当前库 `integrity_check=ok`、`foreign_key_check=[]`；迁移 21/22/23。
- 当前资料目录为空，同卷旧数据库备份为 0；Volume 仅有新工作库及运行时正常生成的新 WAL/SHM。

## 6. 历史知识与页面验收

实际 API GET `/businesses`、`/systems`、`/incidents`、`/runs`、`/patterns`、`/knowledge-counterexamples`、`/evaluation-cases`、`/evaluations` 均返回 HTTP 200 和空列表。旧业务 ID 查询为 404。模型能力接口仍显示原模型。

FTS 对 `Catalog-Test`、`Catalog-Mock`、`catalog-api`、`store` 的 MATCH 查询均为 0。源码索引、业务事实、图谱节点/边/快照、Pattern、经验、调查、反证、预算和模型用量表均为 0。Agent 画像/图谱/历史反例及 Pattern 读取来自这些工作库表，没有另一个持久知识库需要迁移。API 进程重新启动，旧内存状态和会话 SSH 凭据也已清除。

实际 Chrome 浏览器验证首页“尚未登记系统”、业务选择只有空项、新业务表单可见、业务列表为空、服务器范围提示先创建业务；无 JavaScript 错误，无外部请求。浏览器验收禁止所有写请求，未向新工作库提交业务。

截图：`homepage-empty.png`、`new-business-empty.png`，位于本轮证据目录。

## 7. SSH、模型及开发成果保留

48,416 个既有文件逐一校验未改变，Git 分支仍为 `fix/litellm-runtime-isolation`，HEAD 仍为 `0b2ed815d259b84500bafb484c69e0fc29c48be9`。既有未提交源码、旧测试报告、OpenSSH Fixture、scripts/tests/docs/test-results 和 Git 历史均保留。

本轮新增交付仅为本报告；执行与验收辅助脚本、日志位于被 Git 忽略的本轮 test-results 目录。应用源码、前端、Compose、`.env` 均未修改。所有原容器、镜像 ID、回滚镜像标签、挂载、端口及网络经过比对保留。

- SSH 双认证 `password` / `private_key`、双执行模式 `standard_ssh_readonly` / `forced_gateway`、30 分钟内存凭据、13 个固定只读工具、身份检查、人工确认服务范围及工具授权机制保留。
- 新目标 Docker 读取仍默认开启只读，AI 仍可调用已授权只读工具。
- 严格 Schema、证据门、假设生命周期、独立模型 Worker、超时和错误治理均未改动。
- 模型：`gpt-6.1-sol`；供应商：OpenAI-compatible；现有 Base URL 保留；`reasoning_effort=low`；超时 120 秒；调查 8 轮、工具预算 20；现有 Token 预算与其他环境参数完整保留。
- 全部容器环境变量使用摘要比较（包含密钥但不输出原值），前后一致。
- 实际工作环境 `SSH_ENABLED=false` 保持原值。可以建立业务档案；正式 SSH 接入仍需在下一阶段按原流程明确启用和核对服务器范围。清理没有擅自启用真实 SSH。

## 8. 测试结果

- 实际工作库：完整性、外键、空业务表、FTS 和资料目录检查通过。
- 实际 API/Web：200 / 200，API Docker health=healthy。
- 实际浏览器：4 个页面检查通过，8 个 API 列表为空，工作库写入 0。
- 独立 Docker Mock：使用当前 API 镜像、`--network none`，在本轮证据目录的独立 SQLite 中创建“随想记”、执行 Mock host.info、确认任意 Shell 工具 denied、确认 Docker 只读默认值为 true。工作 Volume 未挂入该容器；真实模型和生产 SSH 均为 0。
- 原 67 passed OpenSSH 集成证据完整保留；本轮不重跑或连接真实服务器。SSH 双认证/执行方式回归由下面完整测试覆盖。
- 完整回归：**378 passed**，0 failed / 0 error / 0 skipped；控制台 568.50 秒，JUnit 568.460 秒。所有测试使用新建的独立临时目录，外部模型禁用、模型密钥为空。SSH 相关测试分布：tests.e2e.test_ssh_password_ui=1、tests.e2e.test_standard_ssh_ui=1、tests.test_ssh_password=20、tests.test_ssh_protocol=3、tests.test_standard_ssh=52。

隔离 Mock 验收脚本的最初请求字段、HTTP 状态及异步结果假设与现有 API 契约不一致，经读取实际接口纠正后最终通过；失败日志仍保留。一次辅助脚本 lint 的范围误包含本轮新生成的合成 fixture 副本，随后收窄到 4 个辅助脚本；既有源码与测试资产没有修改，最终回归结果以完整 XML 为准。

- 应用源码、scripts、tests 的 Ruff 检查通过；git diff --check 退出码 0（仅有既有 CRLF 转 LF 提示，不是内容错误）。

## 9. 回滚、风险和下一步

依操作者最新要求，本次未保留原业务数据备份，**无法提供完整业务数据回滚命令，也没有声称演练过恢复**。回滚镜像仍存在，但镜像只能恢复应用版本，不能恢复删除的业务记录。历史开发测试资产保留在原位置，仅作为开发/测试证据；它们未挂入当前工作库或资料根，不会被新业务的历史知识检索读取。

下一阶段可在“新业务接入”创建“随想记”并导入经授权资料；再由操作者核对低权限 SSH 账号、主机公钥、用户名密码或私钥认证、授权目录及服务绑定。生产接入和正式业务模型验收本轮均未开展。

本轮未执行 Git push/merge/PR，未连接生产、读取生产密码、修改生产 SSHD、操作生产数据库或调用付费模型。没有删除 Docker Volume、全局 Docker 资源或 E:\ATR 项目。

## 10. 证据入口

`E:\ATR\test-results\safe-business-reset-20261010-224748\`：

- `audit-before.json` / `audit-after.json`：动态结构、外键、逐表计数、行摘要和安全设置。
- `host-before.json` / `host-check-initial.json` / `host-check-final.json`：48,416 个既有文件、全部镜像标签、容器 ID/环境/挂载/网络/端口以及原 Volume 完全保留。
- `initialization-verification.json`：新库、删除范围及文件摘要。
- `cleanliness-verification.json`：空库/索引/迁移/配置验证。
- `health-after.json`、`live-readonly-acceptance.json`、页面截图。
- `isolated-mock-complete/result.json`、`isolated-mock.log` / `.exit`。
- `full-regression.log` / `.xml` / `.exit`、`test-summary.json`：完整回归结果。
- `audit-final.json` / `final-verification.json`：所有测试结束后的工作库再次复核和最终状态。

```ini
RESET_STATUS=completed
BACKUP_STATUS=waived_by_user
BACKUP_PATH=none
BACKUP_VERIFIED=not_applicable
OLD_BUSINESSES=1
NEW_BUSINESSES=0
OLD_INCIDENTS=5
NEW_INCIDENTS=0
OLD_PATTERNS=0
NEW_PATTERNS=0
BUSINESS_KNOWLEDGE_CLEAN=true
SQLITE_INTEGRITY=ok
SQLITE_FOREIGN_KEYS=ok
SSH_MODES_PRESERVED=true
MODEL_CONFIG_PRESERVED=true
DOCKER_VOLUME_PRESERVED=true
GIT_WORKTREE_PRESERVED=true
API_HEALTH=healthy
WEB_HEALTH=200
PRODUCTION_CONNECTIONS=0
REAL_API_CALL_COUNT=0
GIT_PUSHED=false
READY_FOR_SUIXIANGJI=onboarding_ready
```
