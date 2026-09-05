#!/usr/bin/env bash
# 客服反馈系统 —— 停止服务
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/.server.pid"
SERVICE_LABEL="com.muse-ai.community-insights"

if [[ "$(uname -s)" == "Darwin" ]] \
  && launchctl print "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1; then
  launchctl remove "$SERVICE_LABEL"
  rm -f "$PID_FILE"
  echo "✅ 已停止服务"
elif [[ -f "$PID_FILE" ]] \
  && kill -0 "$(cat "$PID_FILE")" 2>/dev/null \
  && ps -p "$(cat "$PID_FILE")" -o command= | grep -q '[a]pp.py'; then
  kill "$(cat "$PID_FILE")"
  rm -f "$PID_FILE"
  echo "✅ 已停止服务"
else
  rm -f "$PID_FILE"
  echo "服务未在运行"
fi
