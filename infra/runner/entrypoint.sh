#!/bin/sh
set -eu
TASK_CONTRACT=/run/task-contract.json
if [ ! -f "$TASK_CONTRACT" ]; then
  echo "task-contract.json not mounted; refusing to start" >&2
  exit 1
fi
cd /workspaces
exec opencode serve --hostname 127.0.0.1 --port 4096 2>&1
