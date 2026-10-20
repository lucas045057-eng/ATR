# Hackathon Catalog Demo

这是 ATR 黑客松固定演示样本：一个 `catalog-api` 应用依赖 `store` 数据库。

演示故障：`store` 不可用，应用启动检查失败，外部健康请求返回 502。

ATR 的演示顺序：

1. 导入本目录压缩包；
2. 审核 `catalog-api -> store` 依赖；
3. 选择 Mock 目标并确认 `catalog-api`；
4. 创建“外部请求返回 502”事件；
5. 执行只读调查；
6. 查看状态、日志和证据 Gate，得到“数据库不可用导致应用异常”。

故障注入和修复由演示操作者控制，Agent 只读取授权范围内的状态和日志。
