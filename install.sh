#!/usr/bin/env sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")" && pwd)

if [ -n "${PYTHON:-}" ]; then
    python=$PYTHON
elif command -v python3 >/dev/null 2>&1; then
    python=python3
elif command -v python >/dev/null 2>&1; then
    python=python
else
    echo "安装失败：未找到 Python。请安装 Python 3.11 或更高版本。" >&2
    exit 1
fi

if ! "$python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "安装失败：需要 Python 3.11 或更高版本。当前：$("$python" --version 2>&1)" >&2
    exit 1
fi

if ! "$python" -m pip --version >/dev/null 2>&1; then
    echo "安装失败：当前 Python 缺少 pip。请先安装 pip 后重试。" >&2
    exit 1
fi

if ! "$python" -m uv --version >/dev/null 2>&1; then
    echo "未找到 uv，正在安装..."
    if ! "$python" -m pip install uv; then
        echo "安装失败：uv 安装失败。请检查网络，或手动运行 $python -m pip install uv。" >&2
        exit 1
    fi
fi

cd "$root"
echo "正在安装 ATR 依赖（首次需要联网）..."
if ! "$python" -m uv sync --locked; then
    echo "安装失败：依赖安装失败。请检查网络连接和 uv.lock 是否存在。" >&2
    exit 1
fi

echo "正在初始化 ATR..."
if ! PYTHONPATH="$root/backend${PYTHONPATH:+:$PYTHONPATH}" "$python" -m uv run --locked --project "$root" python -m app.cli init; then
    echo "安装失败：初始化失败。请根据上面的错误修复后重试。" >&2
    exit 1
fi

echo ""
echo "安装完成。下一步："
echo "  sh ./scripts/atr serve"
echo "健康检查：http://127.0.0.1:8000/api/v1/health"
echo "已存在的 .env 不会被覆盖；如需修改配置，请参考 docs/deployment-new-machine-runbook.md。"
