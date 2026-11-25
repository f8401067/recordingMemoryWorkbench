#!/bin/bash
# 双击这个文件即可启动 Recall 工作台（macOS 会用「终端」打开它）
cd "$(dirname "$0")" || exit 1
exec /bin/bash scripts/start.sh
