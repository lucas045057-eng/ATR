# 黑客松 Demo 场景

## 固定样本

样本位于 `demo/hackathon-catalog/`，包含两个服务：

- `catalog-api`：对外提供目录接口；
- `store`：应用依赖的 MySQL 数据库。

## 固定故障

停止或模拟 `store` 不可用，应用启动检查失败，外部请求返回 502。ATR 只读取当前授权范围内的服务状态和日志，不执行停止、重启或修复操作。

## 演示结论

`catalog-api` 的异常与 `store` 当前不可用状态、数据库连接失败日志形成当前证据链；如果证据不足，界面必须保持“未解决”，不把模型猜测显示为根因。

## 相关测试

- `tests/test_business.py`：Catalog 样本资料导入和业务图解析；
- `tests/test_runtime.py`：Mock 范围发现、服务审核和只读日志读取；
- `tests/test_investigation.py`：固定故障调查和 Evidence Gate；
- `tests/test_v23_decision_runtime.py`：Runtime 动作约束和结论边界。
