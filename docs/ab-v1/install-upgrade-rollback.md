# ATR A+B v1.1 Docker 离线候选包：安装、升级和回退

当前包用于继续验收，整体状态为 **INCOMPLETE_PREVIEW**。它包含原部署实际运行的 Linux amd64 API/UI 镜像、提交过的源码、锁文件、Archify、Python/Node/OCR/语言解析依赖和许可通知。模型服务、Docker Engine 和 SSH 目标服务器由操作者提供。Windows 原生完整运行时、干净 Windows 11/Ubuntu 24.04 安装尚未完成验收，不能将 Windows 上的 Docker 运行记录视为 Windows 原生安装通过。

## 先校验镜像包

解压到一个新的目录。Windows 使用 PowerShell，Docker Desktop 必须切换到 Linux 引擎：

```powershell
& .\load-images.ps1 -Context desktop-linux
```

Ubuntu 使用已有 Linux Docker Engine：

```sh
sh ./load-images.sh default
```

加载程序校验文件摘要、检查引擎、拒绝覆盖不同内容的同名镜像标签，并验证导入后的镜像身份和 Linux amd64 平台。Docker containerd 存储与经典存储可能分别使用同一归档的 OCI index 摘要或 config 摘要；包记录两者并明确核对，不能接受其他身份。此步骤不会启动或替换容器。`manifest.json` 分别标明源码 SHA 与 API/UI 构建所对应的 SHA；后续只修改交付脚本/文档时，运行镜像 SHA 可以不同。

## 升级原部署

关闭进行中的调查和资料任务。保留原工程目录、`.env`、操作员令牌、SSH 主机公钥和凭据引用；不要用 `.env.example` 覆盖原配置。加密模型设置与检测凭证依赖原操作员令牌，备份必须匹配该令牌。

从 `source.tar.gz` 读取 `scripts/backup_runtime.py`，在原 API 容器的原数据卷中执行备份。以下示例中的路径、唯一备份名与 SHA 均须替换为本次实际值；SHA 是当前运行应用对应的源码版本：

```powershell
Get-Content -Raw .\source\scripts\backup_runtime.py |
  docker --context desktop-linux exec -i atr-runtime-api-1 python - `
    --data /data --destination /data/ab-backups/UNIQUE-BACKUP `
    --application-sha CURRENT-SHA
```

```sh
docker --context default exec -i atr-runtime-api-1 python - \
  --data /data --destination /data/ab-backups/UNIQUE-BACKUP \
  --application-sha CURRENT-SHA < source/scripts/backup_runtime.py
```

脚本要求新的备份目录，拒绝活动任务；保存一致 SQLite 快照、原件清单与 TAR、加密配置、检测凭证和 schema 记录。只有最后生成 `manifest.json` 的目录才是完整备份。中断目录保留供检查，不能作为恢复源。将备份和原 `.env` 另外复制到操作者的私有备份存储；不得放入公开安装包、代码仓库或模型上下文。

同时记录原容器的镜像 ID，并为其保存不会覆盖的唯一回退标签。禁止覆盖以前的回退标签。备份应用 SHA、镜像 ID、数据库与原件摘要必须能互相对应。

以下操作使用原工程 Compose、原 `.env` 和原 `atr-runtime_runtime-data`，只更新原 API/UI。`PROJECT` 为原工程目录，`BUNDLE` 为解压后的镜像包目录：

```powershell
docker --context desktop-linux compose --project-directory PROJECT -p atr-runtime `
  -f PROJECT\compose.yaml -f BUNDLE\compose.images.yaml up -d --no-build --pull never api web
```

```sh
docker --context default compose --project-directory PROJECT -p atr-runtime \
  -f PROJECT/compose.yaml -f BUNDLE/compose.images.yaml up -d --no-build --pull never api web
```

访问 `http://127.0.0.1:28080/`，健康入口为 `http://127.0.0.1:28000/api/v1/health`。核对容器镜像 ID、数据卷名称、原业务/资料/经验数量与可读内容；旧结论保留旧协议。API 重启后 SSH 密码会话失效，须在原页面重新提供；不从备份恢复内存密码。

## 新机器安装

需要已安装的 Linux amd64 Docker Engine/Compose，Windows 可以使用 Docker Desktop 的 Linux 引擎。在一个新的工程目录解开 `source.tar.gz`，自行配置 `.env` 中两个不同的私密令牌；首次安装才从 `.env.example` 创建 `.env`。模型配置保持默认未启用，在页面明确填写自己的服务地址/密钥/模型并做真实文字、JSON、视觉检测。加载上面的离线镜像，再用该工程的 Compose 和镜像覆盖文件启动。无需联网安装 ATR 的 Python、Node、OCR 或解析依赖。模型请求仍需要到所选模型服务的网络；SSH 需要访问明确授权的目标。

此流程尚未在干净的新机器执行；现有原部署验收不能替代它。

## 回退与新增记录

回退前再次做完整当前备份，并增加 `--compare /data/ab-backups/PRE-UPGRADE/runtime.sqlite3`。生成的私有 `new-records.json` 保存升级后新增或改变的行；SQLite BLOB 使用 `{"$sqlite_blob_base64":"..."}` 无损编码。该文件含业务数据，不能公开。导出不是自动合并程序，旧应用不应直接读取新版本行。

先停止原 API/UI；验证待恢复备份的数据库、原件 TAR、模型配置和 manifest 摘要，以及匹配的原 `.env` 和回退镜像 ID。完整保留当前数据后，将匹配备份恢复到原数据卷。数据库恢复必须同时处理旧 WAL/SHM，不能覆盖正在打开的 SQLite，也不能让升级后的 WAL 混入旧快照。恢复文件应保留原容器 UID 10001 的读写权限；不要删除当前资料或备份。再使用明确指定回退镜像 ID 的 Compose 覆盖文件启动原 API/UI，并重新核对健康、对象数量、旧结论与原件。

**目前完整数据库/应用回退演练仍未完成，恢复段是执行要求，不能记为 PASS。** 远端只读网关的实测回退见随源码的 `docs/ab-v1/gateway-upgrade.md`；它不等同于本地数据库恢复。

## 构建与当前验收限制

`scripts/package_release.py` 从干净的已提交源码创建包，验证 API/UI 代码与镜像构建 SHA 相同，导出原运行镜像；不收集原 `.env`、数据库、上传原件、密码会话或未跟踪的本地文件。`runtime-inventory.json` 列出镜像内实际版本，`runtime-notices/` 与源码中的 `third_party/` 保存已带入的第三方通知。

已经在原部署执行的子场景和首次失败/复验记录保留在本次验收台账。整体 V01–V30 仍有未执行项，包括完整故障链、普通 SSH 凭据矩阵、完整回退、新机器安装、指定视口和目标用户五项操作。全量旧浏览器回归因启动临时部署被停止，不能报为通过；继续验收采用用户要求的原部署。
