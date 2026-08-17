#!/bin/zsh
set -euo pipefail

LABEL="com.local.qwen38-ollama"
COMPAT_LABEL="com.local.qwen38-ollama-compat"
DOMAIN="gui/$(id -u)"
API="http://127.0.0.1:11439"
CLAUDE_API="http://127.0.0.1:11440"

if ! version_json=$(curl -fsS "$API/api/version" 2>/dev/null); then
  echo '{"running":false,"api":"'$API'","backend":"ollama"}'
  exit 1
fi

PID=$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/pid =/ {print $3; exit}')
COMPAT_PID=$(launchctl print "$DOMAIN/$COMPAT_LABEL" 2>/dev/null | awk '/pid =/ {print $3; exit}')
COMPAT_RUNNING=false
if curl -fsS "$CLAUDE_API/api/version" >/dev/null 2>&1; then
  COMPAT_RUNNING=true
fi
RSS_KIB=0
if [[ -n "${PID:-}" ]]; then
  RSS_KIB=$(ps -o rss= -p "$PID" | tr -d ' ')
fi

QWEN38_VERSION_JSON="$version_json" \
QWEN38_TAGS_JSON=$(curl -fsS "$API/api/tags") \
QWEN38_PS_JSON=$(curl -fsS "$API/api/ps") \
QWEN38_COMPAT_RUNNING="$COMPAT_RUNNING" \
QWEN38_PID="${PID:-0}" \
QWEN38_COMPAT_PID="${COMPAT_PID:-0}" \
QWEN38_RSS_KIB="${RSS_KIB:-0}" \
QWEN38_MODEL="${QWEN38_MODEL:-qwen3.8:27b-mlx}" \
python3 <<'PY'
import json
import os

version = json.loads(os.environ["QWEN38_VERSION_JSON"])["version"]
model = os.environ["QWEN38_MODEL"]
installed = any(m.get("name") == model for m in json.loads(os.environ["QWEN38_TAGS_JSON"])["models"])
loaded = any(m.get("name") == model for m in json.loads(os.environ["QWEN38_PS_JSON"])["models"])
print(json.dumps({
    "running": True,
    "backend": "ollama",
    "version": version,
    "api": "http://127.0.0.1:11439",
    "claude_api": "http://127.0.0.1:11440",
    "claude_compat_running": os.environ["QWEN38_COMPAT_RUNNING"] == "true",
    "model": model,
    "installed": installed,
    "loaded": loaded,
    "pid": int(os.environ["QWEN38_PID"]),
    "compat_pid": int(os.environ["QWEN38_COMPAT_PID"]),
    "server_resident_gib": round(int(os.environ["QWEN38_RSS_KIB"]) / 1048576, 2),
}, ensure_ascii=False))
PY
