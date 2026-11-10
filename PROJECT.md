# ATR

面向本地单操作员的 SRE 故障调查运行时：理解业务资料、授权只读取证、Evidence Gate 判断结论，永不自动修复。

仓库：https://github.com/lucas045057-eng/ATR

技术栈：Python ≥3.11 / uv / FastAPI；React 19 / TypeScript / esbuild / npm。运行方法见 README：`scripts/atr init`，然后 `scripts/atr serve`。前端由 `make build` 构建。

本地工作台依赖 FastAPI，不提供互联网发布。Pages 只发布说明页与 version.json，产物为 dist-site，工作台产物仍为 frontend/dist。

验证：`make check` 保留 uv 锁检查、ruff、全量 pytest、工作台浏览器流程和前端构建，并加入说明页检查和功能复核。外部模型默认关闭，SSH 操作必须经过授权。
