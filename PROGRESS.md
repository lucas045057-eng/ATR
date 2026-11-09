# 接入进度

方案：[#1](https://github.com/lucas045057-eng/ATR/issues/1)。分析与施工基线：`9fc1dbd1eeb9ba7cf64a44b862e893742b37d267`。

## 已完成的施工

已补齐约定文档、13 项已有功能清单、可信施工入口、质量守卫、完整检查入口、静态说明页与独立 Pages 发布配置。原 AGENTS.md 保留并追加约定；既有业务代码、依赖锁文件、ci.yml 与已有测试断言未删改。只追加审核和资料解包反例测试，并调整浏览器测试夹具使强制验收时缺浏览器/前端构建直接失败。

## 本地验证（2026-11-09）

- `make check` 退出 0，保留 uv 锁检查、ruff、全部既有 pytest、前端构建、说明页端到端和功能逐项复核。
- Node 单元测试：21 passed，0 failed / skipped。
- 全量 pytest：1110 passed，0 failed / skipped。此轮全量收集后追加的 8 个资料解包反例，由 F1 验证及 make check 的功能复核再次覆盖，F1 最终 48 passed。
- F1～F13 均由 scripts/verify-feature.sh 写入 passing，并全部通过最终 recheck。
- F1 48、F2 44、F3 32、F4 64、F5 100、F6 141、F7 145、F8 7、F9 4、F10 140、F11 30、F12 46 项测试通过；F13 说明页 HTTP/脚本/版本校验通过。
- 初始环境缺 uv 与 Chromium 运行库，已补齐并核实真实浏览器启动；初次环境失败未记作通过。密钥扫描两处命中均为已核实伪凭据，仅对具体测试文件的具体样本放行。

## 待验收

尚未通过 Pi 独立验证，尚未合并、部署或登记。新增 CI 的 20 分钟上限已实际触发超时；本人已选择「调整后继续」，批准延长至 40 分钟。Pages 来源需由管理员在后续发布前设置为 GitHub Actions，本轮未修改仓库设置。


## 第一次返工（2026-11-09）

原提交 da4589ebe1bc3a9380f04c547df08c16c6490e0d 的 Pi 结论为 FAIL：F1～F11 与 F13 通过；沙箱未提供 Chromium，F12 明确失败而非跳过；make check 还触及入口默认单命令 9 分钟时限。GitHub 的完整 pytest 已通过 1118 项，后续功能复核至 F10 时被 20 分钟上限取消。

本次仅将 check.yml 的检查 job 上限改为获准的 40 分钟，并记录本人决定；业务代码、依赖、测试、功能 verification 与 passing/digest 均未改动。

续接期间验证入口已由维护侧更新：沙箱显式使用 /opt/ms-playwright 的预装浏览器，根目录仍只读、用户目录仍遮蔽；完整检查默认时限现为 60 分钟，超时作为环境问题处理，不消耗返工次数。本轮核实其实际可用性，不覆盖维护侧更新。

本地曾启动完整检查，约 70% 时因本轮环境切换中断，没有完成结果，不能记为通过。按更新后的 import-build，返工只运行相关配置与浏览器环境检查；完整 make check、全部功能 verification 和最终 Pi 判定留给新提交的独立验证。

本轮相关检查结果：`node scripts/lint.mjs` 通过；`node --test tests/lint.test.mjs` 为 6 passed / 0 failed / 0 skipped；检查 job 名、40 分钟上限、完整 make check 入口和强制浏览器开关的断言通过；当前验证入口的真实沙箱 Chromium 渲染探测退出 0。完整独立验收仍未通过，未运行或未完成的全量检查不计为通过。
