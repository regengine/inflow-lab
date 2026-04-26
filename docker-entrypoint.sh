#!/bin/sh
set -e

data_dir="${REGENGINE_DATA_DIR:-/data}"
mkdir -p "$data_dir"
chown -R appuser:appuser "$data_dir" 2>/dev/null || true

exec gosu appuser "$@"
