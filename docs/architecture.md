# 架构导览

backend/app 提供 FastAPI API，业务理解、范围授权、调查 Runtime、Evidence Gate 与经验复用模块共同完成只读调查。connector 负责受授权的 Mock / Docker / SSH 取证；collector 提供隔离采集。frontend 是本地 React 工作台，调用本地 API。

详细运行时架构以 [ATR V2.3 决策运行时架构](ATR-V2.3-DECISION-RUNTIME-ARCHITECTURE.md) 为准。site 是独立的静态说明页，通过 version.json 显示部署提交，不包含工作台。
