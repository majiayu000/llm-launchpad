#!/bin/zsh
set -euo pipefail

LABEL="com.local.qwen38-ollama"
COMPAT_LABEL="com.local.qwen38-ollama-compat"
DOMAIN="gui/$(id -u)"
API="http://127.0.0.1:11439"
COMPAT_SETTINGS=("${(@0)$(python3 - "$HOME/Library/LaunchAgents/$COMPAT_LABEL.plist" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

installed = Path(sys.argv[1])
environment = {"QWEN38_COMPAT_HOST": "127.0.0.1", "QWEN38_COMPAT_TOKEN": "ollama"}
if installed.exists():
    environment = plistlib.loads(installed.read_bytes())["EnvironmentVariables"]
sys.stdout.write("\0".join(os.environ.get(name, environment[name]) for name in (
    "QWEN38_COMPAT_HOST", "QWEN38_COMPAT_TOKEN",
)))
PY
)}")
COMPAT_HOST="${COMPAT_SETTINGS[1]}"
QWEN38_COMPAT_TOKEN="${COMPAT_SETTINGS[2]}"
if [[ -z "$COMPAT_HOST" || "$COMPAT_HOST" == "0.0.0.0" ]]; then
  COMPAT_HOST="127.0.0.1"
fi
CLAUDE_API="http://$COMPAT_HOST:11440"

if ! version_json=$(curl -fsS "$API/api/version" 2>/dev/null); then
  echo '{"running":false,"api":"'$API'","backend":"ollama"}'
  exit 1
fi

PID=$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/pid =/ {print $3; exit}')
COMPAT_PID=$(launchctl print "$DOMAIN/$COMPAT_LABEL" 2>/dev/null | awk '/pid =/ {print $3; exit}')
COMPAT_RUNNING=false
if curl -fsS -H "x-api-key: $QWEN38_COMPAT_TOKEN" "$CLAUDE_API/api/version" >/dev/null 2>&1; then
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
QWEN38_CLAUDE_API="$CLAUDE_API" \
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
    "claude_api": os.environ["QWEN38_CLAUDE_API"],
    "claude_compat_running": os.environ["QWEN38_COMPAT_RUNNING"] == "true",
    "model": model,
    "installed": installed,
    "loaded": loaded,
    "pid": int(os.environ["QWEN38_PID"]),
    "compat_pid": int(os.environ["QWEN38_COMPAT_PID"]),
    "server_resident_gib": round(int(os.environ["QWEN38_RSS_KIB"]) / 1048576, 2),
}, ensure_ascii=False))
PY
