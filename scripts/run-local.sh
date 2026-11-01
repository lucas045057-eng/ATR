#!/usr/bin/env sh
set -eu
exec sh "$(dirname "$0")/atr" serve "$@"
