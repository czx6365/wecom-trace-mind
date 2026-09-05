#!/usr/bin/env bash
# 客服反馈系统 —— 一键启动（后台运行 + 自动打开浏览器）
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PID_FILE="$DIR/.server.pid"
LOG_FILE="$DIR/.server.log"
SERVICE_LABEL="com.muse-ai.community-insights"

# Web 服务只依赖标准库和本地模块；企业微信采集 Python 由 .env 的 WECOM_PYTHON 单独控制。
PY="$(command -v python3)"

service_pid() {
  launchctl print "gui/$(id -u)/$SERVICE_LABEL" 2>/dev/null \
    | awk '$1 == "pid" && $2 == "=" { print $3; exit }'
}

is_running() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    launchctl print "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1
  else
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
  fi
}

# 从 .runtime.json 读取服务真实地址（app.py 启动后会写入）
get_url() {
  [[ -f "$DIR/.runtime.json" ]] || return 1
  "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$DIR/.runtime.json" 2>/dev/null
}

open_browser() {
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$url"
  else
    echo "请手动打开：$url"
  fi
}

# 已在运行？直接打开页面，不重复启动
if is_running; then
  echo "✅ 服务已在运行"
  if URL="$(get_url)"; then
    open_browser "$URL"
  fi
  exit 0
fi

# 首次运行：生成 .env 模板
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "⚠️  已生成 .env，请先填写 MODEL_API_URL / MODEL_API_KEY 后再执行 ./start.sh"
  exit 1
fi

# 后台启动。macOS 交给 launchd 托管，避免启动终端退出后子进程被回收。
rm -f "$DIR/.runtime.json"
if [[ "$(uname -s)" == "Darwin" ]]; then
  launchctl remove "$SERVICE_LABEL" >/dev/null 2>&1 || true
  launchctl submit -l "$SERVICE_LABEL" -o "$LOG_FILE" -e "$LOG_FILE" -- "$PY" "$DIR/app.py"
  PID="$(service_pid)"
else
  nohup "$PY" "$DIR/app.py" > "$LOG_FILE" 2>&1 &
  PID=$!
fi

if [[ -n "$PID" ]]; then
  echo "$PID" > "$PID_FILE"
else
  rm -f "$PID_FILE"
fi

# 等待服务就绪
URL=""
for _ in {1..30}; do
  if URL="$(get_url)"; then
    if "$PY" - "$URL" >/dev/null 2>&1 <<'PY'
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=1) as response:
    if response.status >= 400:
        raise SystemExit(1)
PY
    then
      break
    fi
  fi
  if ! is_running; then
    echo "❌ 启动失败，最近日志："
    tail -n 20 "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
  fi
  URL=""
  sleep 0.5
done

if [[ -z "$URL" ]]; then
  echo "⚠️  等待超时，最近日志："
  tail -n 20 "$LOG_FILE"
  exit 1
fi

PID="$(service_pid 2>/dev/null || true)"
if [[ -n "$PID" ]]; then
  echo "$PID" > "$PID_FILE"
fi

echo "✅ 客服反馈系统已启动：$URL"
echo "   日志：$LOG_FILE  |  停止：./stop.sh"
open_browser "$URL"
