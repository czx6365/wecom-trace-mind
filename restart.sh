#!/usr/bin/env bash
# 客服反馈系统 —— 重启服务
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$DIR/stop.sh"
"$DIR/start.sh"
