#!/usr/bin/env bash
# 跑某个功能项的验证命令，并据结果写 features.json：成功 → state=passing（同时写 verified.digest）。
# 这是把状态改成 passing 的【唯一】途径；手工改 features.json 的 state 会被 make check 拦住。
#   bash scripts/verify-feature.sh F1
#   bash scripts/verify-feature.sh --list        列出所有功能项
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

case "${1:-}" in
  "" | -h | --help) echo "用法：bash scripts/verify-feature.sh <功能项id，如 F1> | --list" >&2; exit 2 ;;
  --list) exec node scripts/features.mjs list ;;
  *) exec node scripts/features.mjs verify "$1" ;;
esac
